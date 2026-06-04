import uuid
import paramiko
from typing import Generator, Optional

from app.database import SyncSessionLocal
from app.models.task_log import TaskLog

class SSHExecutor:
    """
    Handles SSH execution on remote nodes for Celery workers.
    
    Why Paramiko instead of AsyncSSH?
    Because Celery workers are fundamentally synchronous block-and-wait processes.
    Using paramiko perfectly aligns with Celery's model.
    """
    
    def __init__(self, ip: str, username: str, key_filename: Optional[str] = None):
        import os
        self.ip = ip
        self.username = username
        self.key_filename = os.path.expanduser(key_filename) if key_filename else None
        
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
    def __enter__(self):
        # We rely on SSH agent or key_filename for auth
        self.client.connect(
            hostname=self.ip,
            username=self.username,
            key_filename=self.key_filename,
            timeout=10
        )
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.client.close()

    def run_command(self, command: str, task_id: uuid.UUID) -> int:
        """
        Executes a command and streams its stdout/stderr line-by-line
        directly into the PostgreSQL task_logs table.
        
        Returns the exit code.
        """
        # get_pty=True merges stderr into stdout, providing a unified stream
        stdin, stdout, stderr = self.client.exec_command(command, get_pty=True)
        
        # We stream the logs directly to the DB so the SSE endpoint can pick them up
        with SyncSessionLocal() as session:
            for line in iter(stdout.readline, ""):
                clean_line = line.strip('\r\n')
                if not clean_line:
                    continue
                
                log = TaskLog(
                    task_id=task_id,
                    line=clean_line
                )
                session.add(log)
                session.commit()  # Commit per line so UI sees it instantly

        exit_status = stdout.channel.recv_exit_status()
        return exit_status
        
    def run_command_quiet(self, command: str) -> str:
        """
        Executes a command and returns the stdout string without writing to task_logs.
        Useful for gathering specs (like nvidia-smi) silently.
        """
        stdin, stdout, stderr = self.client.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        
        if exit_status != 0:
            err = stderr.read().decode('utf-8')
            raise RuntimeError(f"Command failed with exit {exit_status}: {err}")
            
        return stdout.read().decode('utf-8').strip()
