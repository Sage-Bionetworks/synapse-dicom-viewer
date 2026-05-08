import json
import time
from abc import ABC, abstractmethod

import requests
import synapseclient

BUFFER_SECS = 60  # Refresh this many seconds before expiry

_SYNAPSE_REPO = "https://repo-prod.prod.sagebase.org/repo/v1"
_SYNAPSE_FILE = "https://file-prod.prod.sagebase.org/file/v1"


class BaseRefreshingUrl(ABC):
    """Base class for presigned URL managers with caching and auto-refresh."""

    def __init__(self, object_id: str, expiry_secs: int):
        self.object_id = object_id
        self._expiry_secs = expiry_secs
        self._url: str | None = None
        self._fetched_at: float = 0.0

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._fetched_at) >= (self._expiry_secs - BUFFER_SECS)

    @abstractmethod
    def _fetch(self) -> str: ...

    def get(self) -> str:
        if self._url is None or self._is_stale():
            self._url = self._fetch()
            self._fetched_at = time.monotonic()
        return self._url

    def invalidate(self):
        self._url = None

    def __call__(self) -> str:
        return self.get()


class SynapseRefreshingUrl(BaseRefreshingUrl):
    """Presigned URL manager for Synapse entities.

    Accepts either:
    - A synapseclient.Synapse instance (local mode)
    - A callable returning a token string (hosted mode — plain REST API,
      no long-lived client objects)
    """

    def __init__(self, entity_id: str, syn_or_token_factory):
        super().__init__(entity_id, expiry_secs=900)  # 15 min
        if isinstance(syn_or_token_factory, synapseclient.Synapse):
            self._syn = syn_or_token_factory
            self._token_factory = None
        else:
            self._syn = None
            self._token_factory = syn_or_token_factory

    def _fetch(self) -> str:
        if self._syn is not None:
            return self._fetch_via_client(self._syn)
        return self._fetch_via_rest(self._token_factory())

    def _fetch_via_client(self, syn: synapseclient.Synapse) -> str:
        """Fetch presigned URL using synapseclient (local mode)."""
        print(f"[refresh] fetching new presigned URL for {self.object_id} (Synapse)")
        entity = syn.restGET(f"/entity/{self.object_id}")
        file_handle_id = entity["dataFileHandleId"]
        response = syn.restPOST(
            "/fileHandle/batch",
            body=json.dumps({
                "requestedFiles": [{
                    "fileHandleId": file_handle_id,
                    "associateObjectId": self.object_id,
                    "associateObjectType": "FileEntity",
                }],
                "includePreSignedURLs": True,
                "includeFileHandles": False,
                "includePreviewPreSignedURLs": False,
            }),
            endpoint=syn.fileHandleEndpoint,
        )
        files = response.get("requestedFiles", [])
        if not files or "preSignedURL" not in files[0]:
            raise RuntimeError(
                f"No presigned URL returned for {self.object_id}. "
                f"Check entity permissions and file handle status."
            )
        return files[0]["preSignedURL"]

    def _fetch_via_rest(self, token: str) -> str:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        print(f"[refresh] fetching new presigned URL for {self.object_id} (Synapse REST)")

        # 1. Get entity metadata to find the file handle ID
        r = requests.get(f"{_SYNAPSE_REPO}/entity/{self.object_id}", headers=headers, timeout=15)
        r.raise_for_status()
        file_handle_id = r.json()["dataFileHandleId"]

        # 2. Get presigned URL via batch file handle API
        r = requests.post(
            f"{_SYNAPSE_FILE}/fileHandle/batch",
            headers=headers,
            json={
                "requestedFiles": [{
                    "fileHandleId": file_handle_id,
                    "associateObjectId": self.object_id,
                    "associateObjectType": "FileEntity",
                }],
                "includePreSignedURLs": True,
                "includeFileHandles": False,
                "includePreviewPreSignedURLs": False,
            },
            timeout=15,
        )
        r.raise_for_status()
        files = r.json().get("requestedFiles", [])
        if not files or "preSignedURL" not in files[0]:
            raise RuntimeError(
                f"No presigned URL returned for {self.object_id}. "
                f"Check entity permissions and file handle status."
            )
        return files[0]["preSignedURL"]
        # token goes out of scope here — not retained anywhere


class Gen3RefreshingUrl(BaseRefreshingUrl):
    """Presigned URL manager for Gen3/DRS objects.

    Accepts a full DRS URI (drs://host/object_id) or a bare object ID.
    The endpoint and auth can be provided explicitly or parsed from the URI.
    """

    def __init__(self, drs_uri: str, default_endpoint: str | None = None, default_auth=None, auth_factory=None):
        parsed = self.parse_drs_uri(drs_uri)
        if parsed:
            self._endpoint = f"https://{parsed[0]}"
            object_id = parsed[1]
        else:
            self._endpoint = default_endpoint
            object_id = drs_uri
        super().__init__(object_id, expiry_secs=3600)  # Gen3 URLs typically 1 hour
        self._auth = default_auth
        self._auth_factory = auth_factory

    @staticmethod
    def parse_drs_uri(uri: str) -> tuple[str, str] | None:
        """Parse drs://host/object_id → (host, object_id) or None."""
        if not uri.startswith("drs://"):
            return None
        rest = uri[len("drs://"):]
        slash = rest.find("/")
        if slash < 0:
            return None
        return rest[:slash], rest[slash + 1:]

    def _fetch(self) -> str:
        from gen3.file import Gen3File

        auth = self._auth_factory() if self._auth_factory else self._auth
        print(f"[refresh] fetching new presigned URL for {self.object_id} (Gen3: {self._endpoint})")
        file_client = Gen3File(self._endpoint, auth)
        result = file_client.get_presigned_url(self.object_id, protocol="s3")
        if not result or "url" not in result:
            raise RuntimeError(
                f"No presigned URL returned for {self.object_id}. "
                f"Check Gen3 credentials and object permissions."
            )
        return result["url"]


# Backward-compatible alias — proxy and tests use this name
RefreshingUrl = SynapseRefreshingUrl


def range_fetch(getter: BaseRefreshingUrl, offset: int, length: int) -> bytes:
    """Issue a single byte-range GET. Retries once on 403 with a fresh URL."""
    url = getter()
    headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 403:
        getter.invalidate()
        url = getter()
        r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.content
