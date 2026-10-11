"""Client for the Harbor Hub: the read-only viewer API plus job copies.

Every call routes through
:func:`sprint.auth.client.create_authenticated_client`, so it works
transparently with both an interactive session and ``HARBOR_API_KEY`` auth.
The viewer RPCs (:class:`HubClient`) are wrapped in
:func:`sprint.auth.retry.supabase_rpc_retry`; :func:`copy_job` drives the
``job-copy`` edge function and is resumable instead (re-invoking converges).
"""

from sprint.hub.client import HubClient
from sprint.hub.copy import CopyJobResult, copy_job, copy_trial

__all__ = ["CopyJobResult", "HubClient", "copy_job", "copy_trial"]
