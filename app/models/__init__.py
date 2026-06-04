# Import the Base class
from app.database import Base

# Import all models here so Alembic can discover them automatically
# when it imports Base from this module.
from app.models.benchmark_result import BenchmarkResult
from app.models.node import Node
from app.models.task import Task
from app.models.task_log import TaskLog
from app.models.workload import Workload
from app.models.workload_event import WorkloadEvent
from app.models.workload_type import WorkloadType

# This ensures that `Base.metadata` contains all the tables.
