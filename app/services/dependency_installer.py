import uuid
from app.services.manifest_builder import _GCR_LOGIN_CMD
from app.services.ssh_executor import SSHExecutor


class DependencyInstaller:
    """
    Installs required dependencies on the GPU node via SSH.
    Pulls the workload container image from the public GCR registry.
    """

    @staticmethod
    def install_vllm(ip: str, username: str, task_id: uuid.UUID) -> bool:
        from app.config import settings, get_workload_registry

        image = "%s/llminference:%s" % (get_workload_registry(), settings.WORKLOAD_IMAGE_TAG)

        script = (
            'echo "Checking Docker..." && '
            'if ! command -v docker > /dev/null 2>&1; then '
            '  echo "Docker not found. Please install Docker first." && exit 1; '
            'fi && '
            'echo "Authenticating with GCR..." && '
            '%s && '
            'echo "Pulling workload image: %s" && '
            'docker pull %s && '
            'echo "Image ready."'
        ) % (_GCR_LOGIN_CMD, image, image)

        with SSHExecutor(ip, username, key_filename=settings.SSH_KEY_PATH) as ssh:
            exit_code = ssh.run_command(script, task_id)
            return exit_code == 0
