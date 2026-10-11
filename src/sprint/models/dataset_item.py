from pathlib import Path

from pydantic import BaseModel

from sprint.models.task.id import GitTaskId, LocalTaskId, PackageTaskId


class DownloadedDatasetItem(BaseModel):
    id: GitTaskId | LocalTaskId | PackageTaskId
    downloaded_path: Path
