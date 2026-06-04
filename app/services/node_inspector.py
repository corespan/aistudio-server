from app.config import settings
import json
import logging
from app.services.ssh_executor import SSHExecutor

logger = logging.getLogger(__name__)

class NodeInspector:
    """
    Connects to a node via SSH to inspect hardware capabilities,
    primarily looking at GPUs via nvidia-smi.
    """
    
    @staticmethod
    def inspect(ip: str, username: str) -> dict:
        """
        Runs nvidia-smi and other checks to gather hardware specs.
        Returns a dictionary representing the JSONB 'specs' column.
        """
        specs = {
            "gpus": [],
            "driver_version": "unknown",
            "cuda_version": "unknown"
        }
        
        try:
            with SSHExecutor(ip, username, key_filename=settings.SSH_KEY_PATH) as ssh:
                # Get basic GPU info in CSV format: index, name, memory.total
                out = ssh.run_command_quiet(
                    "nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader,nounits"
                )
                
                for line in out.splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 3:
                        specs["gpus"].append({
                            "index": int(parts[0]),
                            "name": parts[1],
                            "memory_mb": int(parts[2])
                        })
                
                # Get driver and CUDA version
                out_versions = ssh.run_command_quiet("nvidia-smi")
                # Very basic parsing. In production, we use regex.
                for line in out_versions.splitlines():
                    if "Driver Version:" in line:
                        # Extract "Driver Version: 535.104"
                        parts = line.split("Driver Version:")[1].strip().split()
                        if parts:
                            specs["driver_version"] = parts[0]
                    if "CUDA Version:" in line:
                        parts = line.split("CUDA Version:")[1].strip().split()
                        if parts:
                            specs["cuda_version"] = parts[0]
                        
        except Exception as e:
            logger.error(f"Failed to inspect node {ip}: {e}")
            raise RuntimeError(f"Node inspection failed: {e}")
            
        return specs
