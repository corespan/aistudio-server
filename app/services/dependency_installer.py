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

        # Pull the image in the background and print a heartbeat every 10s so
        # the SSE stream stays alive (docker pull uses \r not \n for progress,
        # which blocks readline() and makes the UI appear frozen).
        script = (
            'echo "Checking Docker..." && '
            'if ! command -v docker > /dev/null 2>&1; then '
            '  echo "Docker not found. Please install Docker first." && exit 1; '
            'fi && '
            'echo "Authenticating with GCR..." && '
            '%(login)s && '
            'echo "Pulling workload image: %(image)s" && '
            'docker pull %(image)s > /tmp/docker_pull.log 2>&1 & '
            'PULL_PID=$! && '
            'while kill -0 $PULL_PID 2>/dev/null; do '
            '  LAYERS=$(grep -c "Pull complete" /tmp/docker_pull.log 2>/dev/null || echo 0); '
            '  echo "Pulling... ($LAYERS layer(s) ready)"; '
            '  sleep 10; '
            'done && '
            'wait $PULL_PID && PULL_EXIT=$? && '
            'cat /tmp/docker_pull.log && '
            '[ $PULL_EXIT -eq 0 ] && echo "Image ready." || '
            '{ echo "Pull failed (exit $PULL_EXIT)"; exit $PULL_EXIT; }'
        ) % {"login": _GCR_LOGIN_CMD, "image": image}

        with SSHExecutor(ip, username, key_filename=settings.SSH_KEY_PATH) as ssh:
            exit_code = ssh.run_command(script, task_id)
            return exit_code == 0
