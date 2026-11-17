"""Worker isolation's Windows form: a Job Object that holds a worker and every process it starts.

`poser.adapters.isolation` imports this module only on Windows. The standard library has no Job
Object API, so this module calls kernel32 through `ctypes`. It declares every function's argument
and result types, so 64-bit handles are not truncated. The structures follow their Microsoft Learn
definitions: JOBOBJECT_BASIC_LIMIT_INFORMATION, JOBOBJECT_EXTENDED_LIMIT_INFORMATION, IO_COUNTERS,
JOBOBJECT_BASIC_PROCESS_ID_LIST, and THREADENTRY32.
"""

import ctypes
import sys
import time
from ctypes import wintypes

assert sys.platform == "win32"  # noqa: S101 - narrows the platform for type checkers.

JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
THREAD_SUSPEND_RESUME = 0x0002
TH32CS_SNAPTHREAD = 0x00000004
ERROR_NO_MORE_FILES = 18
ERROR_MORE_DATA = 234
RESUME_FAILED = 0xFFFFFFFF
WAIT_FAILED = 0xFFFFFFFF
NOT_INHERITED = False
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
KILLED_EXIT_CODE = 1
KILL_WAIT_S = 10.0
FIRST_ID_CAPACITY = 16


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class _IoCounters(ctypes.Structure):
    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


class _ProcessIdListHeader(ctypes.Structure):
    """JOBOBJECT_BASIC_PROCESS_ID_LIST up to its variable-length ProcessIdList, which follows
    it. The empty array makes the size include the padding before the list."""

    _fields_ = (
        ("NumberOfAssignedProcesses", wintypes.DWORD),
        ("NumberOfProcessIdsInList", wintypes.DWORD),
        ("ProcessIdList", ctypes.c_size_t * 0),
    )


class _ThreadEntry32(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    )


SIGNATURES = {
    "CreateJobObjectW": (wintypes.HANDLE, (wintypes.LPVOID, wintypes.LPCWSTR)),
    "SetInformationJobObject": (
        wintypes.BOOL,
        (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD),
    ),
    "QueryInformationJobObject": (
        wintypes.BOOL,
        (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD),
    ),
    "AssignProcessToJobObject": (wintypes.BOOL, (wintypes.HANDLE, wintypes.HANDLE)),
    "IsProcessInJob": (wintypes.BOOL, (wintypes.HANDLE, wintypes.HANDLE, wintypes.PBOOL)),
    "TerminateJobObject": (wintypes.BOOL, (wintypes.HANDLE, wintypes.UINT)),
    "OpenProcess": (wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)),
    "OpenThread": (wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)),
    "ResumeThread": (wintypes.DWORD, (wintypes.HANDLE,)),
    "WaitForSingleObject": (wintypes.DWORD, (wintypes.HANDLE, wintypes.DWORD)),
    "CreateToolhelp32Snapshot": (wintypes.HANDLE, (wintypes.DWORD, wintypes.DWORD)),
    "Thread32First": (wintypes.BOOL, (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))),
    "Thread32Next": (wintypes.BOOL, (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))),
    "CloseHandle": (wintypes.BOOL, (wintypes.HANDLE,)),
}


def _load_kernel32() -> ctypes.WinDLL:
    """Load a private kernel32 instance, so these declarations change no other user's."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, (restype, argtypes) in SIGNATURES.items():
        function = getattr(kernel32, name)
        function.restype = restype
        function.argtypes = argtypes
    return kernel32


_kernel32 = _load_kernel32()


def _last_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _opened(handle: int | None) -> int:
    """Return a handle a kernel32 function opened, or raise its error if it returned NULL."""
    if not handle:
        raise _last_error()
    return handle


def _thread_ids(pid: int) -> list[int]:
    """Return the IDs of the threads process `pid` owns."""
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot is None or snapshot == INVALID_HANDLE_VALUE:
        raise _last_error()
    try:
        entry = _ThreadEntry32(dwSize=ctypes.sizeof(_ThreadEntry32))
        found: list[int] = []
        listed = _kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while listed:
            if entry.th32OwnerProcessID == pid:
                found.append(entry.th32ThreadID)
            listed = _kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != ERROR_NO_MORE_FILES:
            raise _last_error()
        return found
    finally:
        _kernel32.CloseHandle(snapshot)


class WorkerJob:
    """An unnamed Job Object that kills every process in it when its last handle closes.

    Only this object holds the handle, and the handle is not inheritable, so the job's processes
    live only as long as poser holds it. Whatever ends poser, the system then closes the handle.
    The processes a member starts join the job, and a process that breaks away from a job nested
    inside this one stays in this one, because this job permits no breakaway.

    A process that a member has another process start for it, such as a COM server or a
    scheduled task, is not the member's descendant and is not in the job. The job controls
    lifetime and is no sandbox: a process running as the same user could duplicate poser's handle
    to it.
    """

    def __init__(self) -> None:
        self._handle = _opened(_kernel32.CreateJobObjectW(None, None))
        self._open = True
        try:
            self._set_limit_flags(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        except BaseException:
            self.close()
            raise

    def start(self, pid: int) -> None:
        """Assign process `pid`, created suspended, to the job, and then resume it.

        A process created suspended has run no code, so nothing it starts escapes the job. The
        caller holds a handle to the process, so `pid` cannot name another process meanwhile.
        """
        process = _opened(
            _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, NOT_INHERITED, pid)
        )
        try:
            if not _kernel32.AssignProcessToJobObject(self._handle, process):
                raise _last_error()
        finally:
            _kernel32.CloseHandle(process)
        # Usually only the primary thread exists. Resuming a thread that is not suspended
        # leaves it unchanged, so a thread injected meanwhile does no harm.
        thread_ids = _thread_ids(pid)
        if not thread_ids:
            raise OSError(f"process {pid} has no thread to resume")
        for thread_id in thread_ids:
            thread = _opened(_kernel32.OpenThread(THREAD_SUSPEND_RESUME, NOT_INHERITED, thread_id))
            try:
                if _kernel32.ResumeThread(thread) == RESUME_FAILED:
                    raise _last_error()
            finally:
                _kernel32.CloseHandle(thread)

    def kill(self) -> None:
        """Terminate every process in the job, and wait up to `KILL_WAIT_S` seconds for each to
        end.

        A process leaves the job as it begins to exit, before its handles close, so this waits on
        the processes themselves, which are signaled only after their handles close. It opens
        them before terminating them, so their IDs cannot name other processes meanwhile.
        """
        members = self._open_members()
        try:
            if not _kernel32.TerminateJobObject(self._handle, KILLED_EXIT_CODE):
                raise _last_error()
            deadline = time.monotonic() + KILL_WAIT_S
            for member in members:
                remaining_ms = max(0, round((deadline - time.monotonic()) * 1000))
                if _kernel32.WaitForSingleObject(member, remaining_ms) == WAIT_FAILED:
                    raise _last_error()
        finally:
            for member in members:
                _kernel32.CloseHandle(member)

    def release(self) -> None:
        """Let the job's processes outlive it: closing the job no longer kills them."""
        self._set_limit_flags(0)

    def close(self) -> None:
        """Close the job's handle, which kills its processes unless `release` ran."""
        if self._open:
            self._open = False
            _kernel32.CloseHandle(self._handle)

    def _set_limit_flags(self, flags: int) -> None:
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = flags
        if not _kernel32.SetInformationJobObject(
            self._handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise _last_error()

    def _open_members(self) -> list[int]:
        """Open a handle to wait on each process in the job. A process that ended since the list
        was read is skipped, and so is a process that has since taken its ID."""
        members: list[int] = []
        try:
            for pid in self._member_ids():
                member = _kernel32.OpenProcess(
                    SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, NOT_INHERITED, pid
                )
                if not member:
                    continue
                members.append(member)
                if not self._holds(member):
                    members.pop()
                    _kernel32.CloseHandle(member)
        except BaseException:
            for member in members:
                _kernel32.CloseHandle(member)
            raise
        return members

    def _holds(self, process: int) -> bool:
        in_job = wintypes.BOOL()
        if not _kernel32.IsProcessInJob(process, self._handle, ctypes.byref(in_job)):
            raise _last_error()
        return bool(in_job.value)

    def _member_ids(self) -> list[int]:
        """Return the IDs of the processes in the job, enlarging the buffer until they fit."""
        header_size = ctypes.sizeof(_ProcessIdListHeader)
        capacity = FIRST_ID_CAPACITY
        while True:
            buffer = ctypes.create_string_buffer(
                header_size + capacity * ctypes.sizeof(ctypes.c_size_t)
            )
            listed = _kernel32.QueryInformationJobObject(
                self._handle,
                JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                ctypes.byref(buffer),
                ctypes.sizeof(buffer),
                None,
            )
            if not listed and ctypes.get_last_error() != ERROR_MORE_DATA:
                raise _last_error()
            header = _ProcessIdListHeader.from_buffer(buffer)
            count = header.NumberOfProcessIdsInList
            if listed and count >= header.NumberOfAssignedProcesses:
                return list((ctypes.c_size_t * count).from_buffer(buffer, header_size))
            capacity = max(2 * capacity, header.NumberOfAssignedProcesses)
