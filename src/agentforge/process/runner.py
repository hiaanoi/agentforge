import os

from agentforge.process.base import ProcessTreeSupervisor
from agentforge.process.posix import PosixProcessGroupSupervisor
from agentforge.process.windows_job import WindowsJobObjectSupervisor


def create_process_tree_supervisor() -> ProcessTreeSupervisor:
    if os.name == "nt":
        return WindowsJobObjectSupervisor()
    return PosixProcessGroupSupervisor()
