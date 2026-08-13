from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    FAILED = 1
    USAGE = 2
    APPROVAL_REQUIRED = 20
    PAUSED = 21
    UNKNOWN = 22
    CONFIGURATION_ERROR = 30

