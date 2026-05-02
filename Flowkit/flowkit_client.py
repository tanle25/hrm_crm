"""
FlowKit API Client — Full-featured wrapper cho Google Flow / Veo 3

Mục tiêu: Server-side client để xây dựng giao diện web.
Người dùng chọn tùy chọn trên UI → gọi client → tự động tạo video hoàn chỉnh.

Features:
  - Tạo project mới hoặc dùng project cũ
  - Upload hình ảnh hoặc AI generate
  - Chọn model (Veo 3.1 i2v / i2v_fl / r2v)
  - Chọn tỷ lệ khung hình (16:9 / 9:16)
  - Chọn material/style (realistic, 3d_pixar, anime...)
  - Characters với reference images
  - Chain scenes (ROOT / CONTINUATION / INSERT)
  - Upscale 4K
  - TTS narration
  - Concat final video
  - Pipeline tự động end-to-end

Usage:
    from flowkit_client import FlowKitClient
    client = FlowKitClient("https://flow.tanflux.tech", "tanflux-flowkit-2026")
"""

import asyncio
import base64
import json
import logging
import time
import os
import mimetypes
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Any

try:
    import httpx
    HTTP_LIB = "httpx"
except ImportError:
    try:
        import aiohttp
        HTTP_LIB = "aiohttp"
    except ImportError:
        raise ImportError("Install httpx or aiohttp: pip install httpx")

# ─── Logging ─────────────────────────────────────────────────

logger = logging.getLogger("flowkit")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(_h)


# ─── Enums ───────────────────────────────────────────────────

class Orientation(str, Enum):
    HORIZONTAL = "HORIZONTAL"  # 16:9
    VERTICAL = "VERTICAL"      # 9:16

class Material(str, Enum):
    REALISTIC = "realistic"
    PIXAR_3D = "3d_pixar"
    ANIME = "anime"
    STOP_MOTION = "stop_motion"
    MINECRAFT = "minecraft"
    OIL_PAINTING = "oil_painting"

class ChainType(str, Enum):
    ROOT = "ROOT"
    CONTINUATION = "CONTINUATION"
    INSERT = "INSERT"

class EntityType(str, Enum):
    CHARACTER = "character"
    LOCATION = "location"
    CREATURE = "creature"
    VISUAL_ASSET = "visual_asset"
    GENERIC_TROOP = "generic_troop"
    FACTION = "faction"

class RequestType(str, Enum):
    GENERATE_IMAGE = "GENERATE_IMAGE"
    REGENERATE_IMAGE = "REGENERATE_IMAGE"
    EDIT_IMAGE = "EDIT_IMAGE"
    GENERATE_VIDEO = "GENERATE_VIDEO"
    REGENERATE_VIDEO = "REGENERATE_VIDEO"
    GENERATE_VIDEO_REFS = "GENERATE_VIDEO_REFS"
    UPSCALE_VIDEO = "UPSCALE_VIDEO"
    GENERATE_CHARACTER_IMAGE = "GENERATE_CHARACTER_IMAGE"
    REGENERATE_CHARACTER_IMAGE = "REGENERATE_CHARACTER_IMAGE"
    EDIT_CHARACTER_IMAGE = "EDIT_CHARACTER_IMAGE"

class VideoGenMode(str, Enum):
    I2V = "i2v"              # image-to-video (1 start frame)
    I2V_FL = "i2v_fl"        # image-to-video first-last (start + end frame)
    R2V = "r2v"              # reference-to-video (entity refs only)


# ─── Data classes ────────────────────────────────────────────

@dataclass
class CharacterInput:
    name: str
    description: str = ""
    entity_type: str = "character"
    voice_description: Optional[str] = None  # max ~30 words

@dataclass
class SceneInput:
    prompt: str
    video_prompt: str = ""
    image_prompt: Optional[str] = None
    chain_type: str = "ROOT"
    character_names: Optional[list[str]] = None
    transition_prompt: Optional[str] = None   # for i2v_fl CONTINUATION
    narrator_text: Optional[str] = None
    upload_image_path: Optional[str] = None   # local image file to use as scene image
    upload_image_media_id: Optional[str] = None
    upload_image_base64: Optional[str] = None # base64 image data

@dataclass
class SceneResult:
    id: str
    prompt: str
    image_url: Optional[str] = None
    image_media_id: Optional[str] = None
    video_url: Optional[str] = None
    video_media_id: Optional[str] = None
    upscale_url: Optional[str] = None
    status: str = "PENDING"
    error: Optional[str] = None

@dataclass
class VideoResult:
    project_id: str
    video_id: str
    scenes: list[SceneResult] = field(default_factory=list)
    characters: dict = field(default_factory=dict)  # name → {id, media_id, ref_url}
    status: str = "PROCESSING"
    concat_url: Optional[str] = None
    error: Optional[str] = None


def _join_prompt_parts(*parts: Optional[str]) -> str:
    return "\n".join(str(part).strip() for part in parts if str(part or "").strip())


def _flowkit_entity_type(value: Optional[str]) -> str:
    normalized = str(value or "character").strip().lower()
    allowed = {"character", "location", "creature", "visual_asset", "generic_troop", "faction"}
    if normalized in {"product", "brand", "object", "asset"}:
        return "visual_asset"
    return normalized if normalized in allowed else "character"


def _orientation_prefix(orientation: str) -> str:
    return "vertical" if str(orientation or "").upper() == "VERTICAL" else "horizontal"


def _scene_media(scene: dict, orientation: str, media_type: str) -> tuple[Optional[str], Optional[str]]:
    prefix = _orientation_prefix(orientation)
    media_id = (
        scene.get(f"{prefix}_{media_type}_media_id")
    )
    url = (
        scene.get(f"{prefix}_{media_type}_url")
    )
    if prefix == "horizontal":
        media_id = media_id or scene.get(f"{media_type}_media_id") or scene.get("media_id")
        url = url or scene.get(f"{media_type}_url") or scene.get("output_url") or scene.get("url")
    return media_id, url


def _extract_id(payload: dict, *keys: str) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    for nested_key in ("data", "project", "video", "scene", "result"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            value = _extract_id(nested, *keys)
            if value:
                return value
    return None


# ─── Client ─────────────────────────────────────────────────

class FlowKitClient:
    """Full-featured FlowKit API client."""

    def __init__(
        self,
        base_url: str = "https://flow.tanflux.tech",
        api_key: str = "",
        poll_interval: int = 10,
        image_timeout: int = 120,
        video_timeout: int = 300,
        upscale_timeout: int = 600,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.image_timeout = image_timeout
        self.video_timeout = video_timeout
        self.upscale_timeout = upscale_timeout
        self._headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    # ═══════════════════════════════════════════════════════════
    # HTTP
    # ═══════════════════════════════════════════════════════════

    async def _request(self, method: str, path: str, json_data: dict = None) -> Any:
        url = f"{self.base_url}{path}"
        if HTTP_LIB == "httpx":
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.request(method, url, headers=self._headers, json=json_data)
                if r.status_code >= 400:
                    raise Exception(f"HTTP {r.status_code}: {r.text}")
                return r.json() if r.text else {}
        else:
            async with aiohttp.ClientSession() as s:
                async with s.request(method, url, headers=self._headers, json=json_data) as r:
                    if r.status >= 400:
                        raise Exception(f"HTTP {r.status}: {await r.text()}")
                    return await r.json() if r.content_length else {}

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def _post(self, path: str, data: dict = None) -> Any:
        return await self._request("POST", path, data)

    async def _patch(self, path: str, data: dict = None) -> Any:
        return await self._request("PATCH", path, data)

    async def _delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    # ═══════════════════════════════════════════════════════════
    # HEALTH & STATUS
    # ═══════════════════════════════════════════════════════════

    async def health(self) -> dict:
        """Health check — extension connected?"""
        return await self._get("/api/flow/status")

    async def is_connected(self) -> bool:
        """Quick connection check."""
        try:
            s = await self.health()
            return s.get("connected", False)
        except Exception:
            return False

    async def get_credits(self) -> dict:
        """Get tier info and credits."""
        return await self._get("/api/flow/credits")

    async def get_tier(self) -> str:
        """Get current paygate tier."""
        credits = await self.get_credits()
        return credits.get("userPaygateTier", credits.get("data", {}).get("userPaygateTier", "PAYGATE_TIER_ONE"))

    # ═══════════════════════════════════════════════════════════
    # MODELS
    # ═══════════════════════════════════════════════════════════

    async def get_models(self) -> dict:
        """Get current model configuration."""
        return await self._get("/api/models")

    async def set_models(self, config: dict) -> dict:
        """Update model configuration (deep merge)."""
        return await self._patch("/api/models", config)

    # ═══════════════════════════════════════════════════════════
    # MATERIALS
    # ═══════════════════════════════════════════════════════════

    async def list_materials(self) -> list:
        """List all available materials/styles."""
        return await self._get("/api/materials")

    async def get_material(self, material_id: str) -> dict:
        """Get material details."""
        return await self._get(f"/api/materials/{material_id}")

    async def create_material(self, id: str, name: str, style_instruction: str, **kwargs) -> dict:
        """Register a custom material."""
        return await self._post("/api/materials", {
            "id": id, "name": name, "style_instruction": style_instruction, **kwargs
        })

    # ═══════════════════════════════════════════════════════════
    # PROJECTS
    # ═══════════════════════════════════════════════════════════

    async def list_projects(self, status: str = None) -> list:
        """List all projects. Filter by status: ACTIVE/ARCHIVED/DELETED."""
        path = "/api/projects"
        if status:
            path += f"?status={status}"
        return await self._get(path)

    async def get_project(self, project_id: str) -> dict:
        """Get project details."""
        return await self._get(f"/api/projects/{project_id}")

    async def create_project(
        self,
        name: str,
        description: str = "",
        story: str = "",
        material: str = "realistic",
        language: str = "en",
        allow_music: bool = False,
        allow_voice: bool = False,
        characters: list[dict] = None,
    ) -> dict:
        """Create a new project on Google Flow + local DB."""
        data = {
            "name": name,
            "description": description,
            "material": material,
            "language": language,
            "allow_music": allow_music,
            "allow_voice": allow_voice,
        }
        if story:
            data["story"] = story
        if characters:
            data["characters"] = characters
        return await self._post("/api/projects", data)

    async def update_project(self, project_id: str, **kwargs) -> dict:
        """Update project fields."""
        return await self._patch(f"/api/projects/{project_id}", kwargs)

    async def delete_project(self, project_id: str) -> dict:
        return await self._delete(f"/api/projects/{project_id}")

    # ═══════════════════════════════════════════════════════════
    # CHARACTERS / ENTITIES
    # ═══════════════════════════════════════════════════════════

    async def list_characters(self) -> list:
        """List all characters."""
        return await self._get("/api/characters")

    async def get_project_characters(self, project_id: str) -> list:
        """List characters linked to a project."""
        return await self._get(f"/api/projects/{project_id}/characters")

    async def create_character(
        self,
        name: str,
        description: str = "",
        entity_type: str = "character",
        voice_description: str = None,
        image_prompt: str = None,
    ) -> dict:
        """Create a character/entity."""
        data = {"name": name, "description": description, "entity_type": entity_type}
        if voice_description:
            data["voice_description"] = voice_description
        if image_prompt:
            data["image_prompt"] = image_prompt
        return await self._post("/api/characters", data)

    async def update_character(self, character_id: str, **kwargs) -> dict:
        return await self._patch(f"/api/characters/{character_id}", kwargs)

    async def link_character(self, project_id: str, character_id: str) -> dict:
        return await self._post(f"/api/projects/{project_id}/characters/{character_id}")

    async def unlink_character(self, project_id: str, character_id: str) -> dict:
        return await self._delete(f"/api/projects/{project_id}/characters/{character_id}")

    # ═══════════════════════════════════════════════════════════
    # VIDEOS
    # ═══════════════════════════════════════════════════════════

    async def list_videos(self, project_id: str) -> list:
        return await self._get(f"/api/videos?project_id={project_id}")

    async def get_video(self, video_id: str) -> dict:
        return await self._get(f"/api/videos/{video_id}")

    async def create_video_container(self, project_id: str, title: str, **kwargs) -> dict:
        return await self._post("/api/videos", {"project_id": project_id, "title": title, **kwargs})

    async def update_video(self, video_id: str, **kwargs) -> dict:
        return await self._patch(f"/api/videos/{video_id}", kwargs)

    # ═══════════════════════════════════════════════════════════
    # SCENES
    # ═══════════════════════════════════════════════════════════

    async def list_scenes(self, video_id: str) -> list:
        return await self._get(f"/api/scenes?video_id={video_id}")

    async def get_scene(self, scene_id: str) -> dict:
        return await self._get(f"/api/scenes/{scene_id}")

    async def create_scene(
        self,
        video_id: str,
        prompt: str,
        video_prompt: str = "",
        display_order: int = 0,
        chain_type: str = "ROOT",
        character_names: list[str] = None,
        parent_scene_id: str = None,
        image_prompt: str = None,
        transition_prompt: str = None,
        orientation: str = None,
    ) -> dict:
        data = {
            "video_id": video_id,
            "prompt": prompt,
            "display_order": display_order,
            "chain_type": chain_type,
        }
        if orientation:
            data["orientation"] = orientation
        if video_prompt:
            data["video_prompt"] = video_prompt
        if character_names:
            data["character_names"] = character_names
        if parent_scene_id:
            data["parent_scene_id"] = parent_scene_id
        if image_prompt:
            data["image_prompt"] = image_prompt
        if transition_prompt:
            data["transition_prompt"] = transition_prompt
        return await self._post("/api/scenes", data)

    async def update_scene(self, scene_id: str, **kwargs) -> dict:
        return await self._patch(f"/api/scenes/{scene_id}", kwargs)

    async def delete_scene(self, scene_id: str) -> dict:
        return await self._delete(f"/api/scenes/{scene_id}")

    # ═══════════════════════════════════════════════════════════
    # REQUESTS (Job Queue)
    # ═══════════════════════════════════════════════════════════

    async def submit_request(
        self,
        req_type: str,
        project_id: str,
        scene_id: str = None,
        video_id: str = None,
        character_id: str = None,
        orientation: str = None,
        source_media_id: str = None,
    ) -> dict:
        """Submit a single generation request to the worker queue."""
        data = {"type": req_type, "project_id": project_id}
        if scene_id:
            data["scene_id"] = scene_id
        if video_id:
            data["video_id"] = video_id
        if character_id:
            data["character_id"] = character_id
        if orientation:
            data["orientation"] = orientation
        if source_media_id:
            data["source_media_id"] = source_media_id
        return await self._post("/api/requests", data)

    async def submit_batch(self, requests: list[dict]) -> list:
        """Submit multiple requests atomically."""
        return await self._post("/api/requests/batch", {"requests": requests})

    async def get_request(self, request_id: str) -> dict:
        return await self._get(f"/api/requests/{request_id}")

    async def list_requests(self, **filters) -> list:
        """List requests with filters: scene_id, video_id, project_id, status, type."""
        params = "&".join(f"{k}={v}" for k, v in filters.items() if v)
        return await self._get(f"/api/requests?{params}")

    async def get_batch_status(self, video_id: str = None, project_id: str = None,
                                req_type: str = None, orientation: str = None) -> dict:
        """Aggregate status for batch polling."""
        params = {}
        if video_id: params["video_id"] = video_id
        if project_id: params["project_id"] = project_id
        if req_type: params["type"] = req_type
        if orientation: params["orientation"] = orientation
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return await self._get(f"/api/requests/batch-status?{qs}")

    # ═══════════════════════════════════════════════════════════
    # FLOW DIRECT (bypass queue)
    # ═══════════════════════════════════════════════════════════

    async def flow_generate_image(self, prompt: str, project_id: str, **kwargs) -> dict:
        """Generate image directly (bypass queue)."""
        return await self._post("/api/flow/generate-image", {"prompt": prompt, "project_id": project_id, **kwargs})

    async def flow_generate_video(self, start_image_media_id: str, prompt: str,
                                   project_id: str, scene_id: str, **kwargs) -> dict:
        """Generate video directly (bypass queue)."""
        return await self._post("/api/flow/generate-video", {
            "start_image_media_id": start_image_media_id, "prompt": prompt,
            "project_id": project_id, "scene_id": scene_id, **kwargs
        })

    async def flow_generate_video_refs(self, reference_media_ids: list[str], prompt: str,
                                        project_id: str, scene_id: str, **kwargs) -> dict:
        """Generate r2v video from reference images directly."""
        return await self._post("/api/flow/generate-video-refs", {
            "reference_media_ids": reference_media_ids, "prompt": prompt,
            "project_id": project_id, "scene_id": scene_id, **kwargs
        })

    async def flow_upscale(self, media_id: str, scene_id: str, **kwargs) -> dict:
        """Upscale video directly."""
        return await self._post("/api/flow/upscale-video", {"media_id": media_id, "scene_id": scene_id, **kwargs})

    async def flow_upload_image(self, file_path: str, project_id: str = "", file_name: str = "image.png") -> dict:
        """Upload local image to Google Flow."""
        return await self._post("/api/flow/upload-image", {
            "file_path": file_path, "project_id": project_id, "file_name": file_name
        })

    async def upload_image_file(self, file_path: str, project_id: str = "", file_name: str = "image.png") -> dict:
        """Upload an image file through a FlowKit wrapper when available.

        Direct FlowKit agents can only read local paths on their own machine. The wrapper
        endpoint accepts multipart files, which is required when Content Forge and FlowKit
        run in different containers or hosts. If the wrapper endpoint is not present, fall
        back to the legacy path-based API.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Upload image does not exist: {file_path}")
        safe_name = file_name or path.name or "image.png"
        content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        multipart_error: Exception | None = None
        if HTTP_LIB == "httpx":
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    with path.open("rb") as handle:
                        response = await client.post(
                            f"{self.base_url}/upload-image",
                            headers={"X-API-Key": self.api_key},
                            data={"project_id": project_id or ""},
                            files={"file": (safe_name, handle, content_type)},
                        )
                    if response.status_code not in {404, 405}:
                        if response.status_code >= 400:
                            raise Exception(f"HTTP {response.status_code}: {response.text}")
                        return response.json() if response.text else {}
                    multipart_error = Exception(f"HTTP {response.status_code}: {response.text}")
            except Exception as exc:
                multipart_error = exc

        try:
            return await self.flow_upload_image(str(path), project_id, safe_name)
        except Exception as exc:
            if multipart_error:
                raise Exception(f"Image upload failed via multipart ({multipart_error}) and local path fallback ({exc})")
            raise

    async def flow_check_status(self, operations: list[dict]) -> dict:
        return await self._post("/api/flow/check-status", {"operations": operations})

    async def flow_get_media(self, media_id: str) -> dict:
        return await self._get(f"/api/flow/media/{media_id}")

    async def flow_refresh_urls(self, project_id: str) -> dict:
        return await self._post(f"/api/flow/refresh-urls/{project_id}")

    async def flow_edit_image(self, prompt: str, source_media_id: str, project_id: str, **kwargs) -> dict:
        return await self._post("/api/flow/edit-image", {
            "prompt": prompt, "source_media_id": source_media_id, "project_id": project_id, **kwargs
        })

    # ═══════════════════════════════════════════════════════════
    # TTS & NARRATION
    # ═══════════════════════════════════════════════════════════

    async def list_voice_templates(self) -> list:
        return await self._get("/api/tts/templates")

    async def create_voice_template(self, name: str, text: str, language: str = "en", **kwargs) -> dict:
        return await self._post("/api/tts/templates", {"name": name, "text": text, "language": language, **kwargs})

    async def generate_tts(self, text: str, ref_audio: str, output_path: str, language: str = "en") -> dict:
        return await self._post("/api/tts/generate", {
            "text": text, "ref_audio": ref_audio, "output_path": output_path, "language": language
        })

    async def narrate_video(self, video_id: str, ref_audio: str, language: str = "en") -> dict:
        return await self._post("/api/tts/narrate", {
            "video_id": video_id, "ref_audio": ref_audio, "language": language
        })

    # ═══════════════════════════════════════════════════════════
    # POLL HELPERS
    # ═══════════════════════════════════════════════════════════

    async def poll_request(self, request_id: str, timeout: int = None, label: str = "", on_progress: callable = None) -> dict:
        """Poll a request until COMPLETED or FAILED."""
        timeout = timeout or self.video_timeout
        start = time.time()
        while True:
            result = await self.get_request(request_id)
            status = result.get("status")

            if status == "COMPLETED":
                logger.info(f"  ✓ {label} completed ({int(time.time() - start)}s)")
                if on_progress:
                    on_progress(f"{label} completed ({int(time.time() - start)}s)")
                return result

            if status == "FAILED":
                error = result.get("error_message", "Unknown error")
                logger.error(f"  ✗ {label} failed: {error}")
                raise Exception(f"{label} failed: {error}")

            elapsed = time.time() - start
            if elapsed > timeout:
                raise TimeoutError(f"{label} timeout after {timeout}s (status: {status})")

            logger.info(f"  ⏳ {label} {status}... ({int(elapsed)}s)")
            if on_progress:
                on_progress(f"{label} {status}... ({int(elapsed)}s)")
            await asyncio.sleep(self.poll_interval)

    async def poll_batch(self, video_id: str, req_type: str = None, timeout: int = 600) -> dict:
        """Poll batch status until all done."""
        start = time.time()
        while True:
            status = await self.get_batch_status(video_id=video_id, req_type=req_type)
            if status.get("done"):
                return status
            elapsed = time.time() - start
            if elapsed > timeout:
                raise TimeoutError(f"Batch timeout after {timeout}s")
            logger.info(f"  ⏳ Batch: {status.get('completed',0)}/{status.get('total',0)} done ({int(elapsed)}s)")
            await asyncio.sleep(self.poll_interval)

    # ═══════════════════════════════════════════════════════════
    # AUTOMATED PIPELINES
    # ═══════════════════════════════════════════════════════════

    async def generate_video(
        self,
        # ── Project ──
        title: str,
        scenes: list[SceneInput],
        project_id: str = None,              # None = tạo mới, có = dùng project cũ
        description: str = "",
        story: str = "",
        material: str = "realistic",
        language: str = "en",
        allow_music: bool = False,
        allow_voice: bool = False,
        # ── Characters ──
        characters: list[CharacterInput] = None,
        generate_refs: bool = True,          # tạo reference images
        # ── Video settings ──
        orientation: str = "HORIZONTAL",
        video_gen_mode: str = "i2v",         # i2v / i2v_fl / r2v
        upscale_4k: bool = False,
        # ── Callbacks ──
        on_progress: callable = None,        # callback(stage, detail)
    ) -> VideoResult:
        """
        Pipeline tự động end-to-end.

        Stages:
          1. Create/reuse project
          2. Create characters + reference images
          3. Create video container + scenes
          4. Generate scene images (hoặc dùng uploaded images)
          5. Generate videos (i2v / i2v_fl / r2v)
          6. Upscale 4K (optional)

        Args:
            title: Tên video
            scenes: List SceneInput
            project_id: Dùng project cũ (None = tạo mới)
            description: Mô tả project
            story: Câu chuyện (dùng để generate character profiles)
            material: Style — realistic/3d_pixar/anime/stop_motion/minecraft/oil_painting
            language: Ngôn ngữ (en/vi/ja...)
            allow_music: Cho phép background music
            allow_voice: Giữ dialogue trong video audio
            characters: List CharacterInput
            generate_refs: Tạo reference images cho characters
            orientation: HORIZONTAL (16:9) / VERTICAL (9:16)
            video_gen_mode: i2v / i2v_fl / r2v
            upscale_4k: Upscale 4K (TIER_TWO only)
            on_progress: Callback(stage: str, detail: str)
        """

        def _notify(stage: str, detail: str = ""):
            logger.info(f"[{stage}] {detail}")
            if on_progress:
                try:
                    on_progress(stage, detail)
                except Exception:
                    pass

        # ── 0. Health check ──
        _notify("health", "Checking FlowKit connection...")
        if not await self.is_connected():
            raise ConnectionError("FlowKit not connected. Start Chrome + extension + FlowKit agent.")

        orientation = "VERTICAL" if str(orientation or "").upper() in {"VERTICAL", "PORTRAIT", "9:16"} else "HORIZONTAL"
        orientation_instruction = (
            "Create a true vertical 9:16 portrait composition for mobile Reels/Shorts. "
            "Do not place a horizontal 16:9 frame inside a vertical canvas. Fill the entire portrait frame."
            if orientation == "VERTICAL"
            else "Create a true horizontal 16:9 landscape composition. Fill the entire widescreen frame."
        )
        material_instruction = ""
        try:
            material_info = await self.get_material(material)
            material_instruction = material_info.get("scene_prefix") or material_info.get("style_instruction") or ""
        except Exception:
            material_instruction = ""
        char_dicts = None
        # ── 1. Project ──
        if project_id:
            _notify("project", f"Using existing project: {project_id}")
            project = await self.get_project(project_id)
            try:
                await self.update_project(
                    project_id,
                    material=material,
                    language=language,
                    allow_music=allow_music,
                    allow_voice=allow_voice,
                    **({"story": story} if story else {}),
                )
                _notify("project", f"Project settings updated: {material}, {orientation}")
            except Exception as exc:
                _notify("project", f"Project settings update skipped: {exc}")
        else:
            _notify("project", f"Creating project: {title}")
            if characters:
                char_dicts = [
                    {
                        "name": c.name,
                        "description": c.description,
                        "entity_type": _flowkit_entity_type(c.entity_type),
                        **({"voice_description": c.voice_description} if c.voice_description else {}),
                    }
                    for c in characters
                ]
            project = await self.create_project(
                name=title, description=description, story=story,
                material=material, language=language,
                allow_music=allow_music, allow_voice=allow_voice,
                characters=char_dicts,
            )
            project_id = _extract_id(project, "id", "project_id")
            if not project_id:
                raise RuntimeError(f"FlowKit create_project returned no project id: {project}")
            _notify("project", f"Project created: {project_id}")

        result = VideoResult(project_id=project_id, video_id="")

        try:
            # ── 2. Character reference images ──
            if characters and generate_refs and not char_dicts:
                existing_chars = []
                try:
                    existing_chars = await self.get_project_characters(project_id)
                except Exception:
                    existing_chars = []
                existing_names = {str(char.get("name") or "").strip().lower() for char in existing_chars}
                _notify("characters", f"Creating {len(characters)} entities...")
                for c in characters:
                    if c.name.strip().lower() in existing_names:
                        continue
                    char = await self.create_character(
                        name=c.name, description=c.description,
                        entity_type=_flowkit_entity_type(c.entity_type), voice_description=c.voice_description,
                    )
                    await self.link_character(project_id, char["id"])
                    result.characters[c.name] = {"id": char["id"]}
                    existing_names.add(c.name.strip().lower())

            if characters and generate_refs:
                _notify("refs", "Generating reference images...")
                proj_chars = await self.get_project_characters(project_id)
                ref_requests = []
                for char in proj_chars:
                    if char.get("media_id"):
                        _notify("refs", f"  → {char['name']} already has ref, skipping")
                        result.characters[char["name"]] = {
                            "id": char["id"],
                            "media_id": char["media_id"],
                            "ref_url": char.get("reference_image_url"),
                        }
                        continue
                    req = await self.submit_request(
                        req_type="GENERATE_CHARACTER_IMAGE",
                        character_id=char["id"],
                        project_id=project_id,
                    )
                    ref_requests.append((char["name"], char["id"], req["id"]))
                    _notify("refs", f"  → Queued ref for {char['name']}")

                for name, char_id, req_id in ref_requests:
                    ref_result = await self.poll_request(
                        req_id,
                        self.image_timeout,
                        f"Ref {name}",
                        on_progress=lambda detail: _notify("refs", detail),
                    )
                    result.characters[name] = {
                        "id": char_id,
                        "media_id": ref_result.get("media_id"),
                        "ref_url": ref_result.get("output_url"),
                    }

            # ── 3. Video container + scenes ──
            _notify("video", "Creating video container...")
            video = await self.create_video_container(project_id, title, orientation=orientation)
            video_id = video["id"]
            result.video_id = video_id

            _notify("scenes", f"Creating {len(scenes)} scenes...")
            scene_objects = []
            for i, scene in enumerate(scenes):
                chain = scene.chain_type if i > 0 else "ROOT"
                parent_id = None
                if i > 0 and chain == "CONTINUATION":
                    parent_id = scene_objects[-1]["id"]

                sc = await self.create_scene(
                    video_id=video_id,
                    prompt=_join_prompt_parts(material_instruction, orientation_instruction, scene.prompt),
                    video_prompt=_join_prompt_parts(orientation_instruction, scene.video_prompt),
                    display_order=i,
                    chain_type=chain,
                    character_names=scene.character_names,
                    parent_scene_id=parent_id,
                    image_prompt=_join_prompt_parts(material_instruction, orientation_instruction, scene.image_prompt),
                    transition_prompt=scene.transition_prompt,
                    orientation=orientation,
                )
                scene_objects.append(sc)
                result.scenes.append(SceneResult(id=sc["id"], prompt=scene.prompt))
                _notify("scenes", f"  → Scene {i}: {sc['id'][:8]}")

                # Set narrator_text if provided
                if scene.narrator_text:
                    await self.update_scene(sc["id"], narrator_text=scene.narrator_text)

            # ── 4. Scene images ──
            _notify("images", "Generating scene images...")
            image_requests = []
            for i, (scene_input, scene_obj) in enumerate(zip(scenes, scene_objects)):
                # Use a pre-uploaded custom image if provided by the UI.
                if scene_input.upload_image_media_id:
                    media_id = scene_input.upload_image_media_id
                    orient_prefix = "horizontal" if orientation == "HORIZONTAL" else "vertical"
                    await self.update_scene(scene_obj["id"], **{
                        f"{orient_prefix}_image_media_id": media_id,
                        f"{orient_prefix}_image_status": "COMPLETED",
                    })
                    result.scenes[i].image_media_id = media_id
                    result.scenes[i].status = "IMAGE_READY"
                    _notify("images", f"  ✓ Scene {i} image selected: {media_id[:8]}")
                    continue

                # Upload custom image from a local path if provided by scripts.
                if scene_input.upload_image_path:
                    _notify("images", f"  → Uploading image for scene {i}")
                    try:
                        upload_result = await self.upload_image_file(
                            scene_input.upload_image_path, project_id
                        )
                        media_id = _extract_id(upload_result, "media_id", "id")
                        if media_id:
                            orient_prefix = "horizontal" if orientation == "HORIZONTAL" else "vertical"
                            await self.update_scene(scene_obj["id"], **{
                                f"{orient_prefix}_image_media_id": media_id,
                                f"{orient_prefix}_image_status": "COMPLETED",
                            })
                            result.scenes[i].image_media_id = media_id
                            result.scenes[i].status = "IMAGE_READY"
                            _notify("images", f"  ✓ Scene {i} image uploaded: {media_id[:8]}")
                            continue
                        _notify("images", f"  ⚠ Scene {i} image upload returned no media_id; generating image from prompt")
                    except Exception as exc:
                        _notify("images", f"  ⚠ Scene {i} image upload unavailable; generating image from prompt instead: {exc}")

                # Generate image via queue
                req = await self.submit_request(
                    req_type="GENERATE_IMAGE",
                    scene_id=scene_obj["id"],
                    video_id=video_id,
                    project_id=project_id,
                    orientation=orientation,
                )
                image_requests.append((i, req["id"]))
                _notify("images", f"  → Queued image for scene {i}")

            for i, req_id in image_requests:
                img_result = await self.poll_request(
                    req_id,
                    self.image_timeout,
                    f"Image scene {i}",
                    on_progress=lambda detail: _notify("images", detail),
                )
                scene_state = await self.get_scene(scene_objects[i]["id"])
                media_id, image_url = _scene_media(scene_state, orientation, "image")
                if not media_id and orientation == "HORIZONTAL":
                    media_id = img_result.get("media_id")
                    image_url = image_url or img_result.get("output_url")
                elif not image_url and orientation == "HORIZONTAL":
                    image_url = img_result.get("output_url")
                result.scenes[i].image_url = image_url
                result.scenes[i].image_media_id = media_id
                if orientation == "VERTICAL" and not scene_state.get("vertical_image_media_id"):
                    _notify("images", f"  ⚠ Scene {i} has no vertical image media; not using horizontal fallback")

            # ── 5. Videos ──
            _notify("videos", f"Generating Veo 3 videos (mode: {video_gen_mode})...")
            video_requests = []

            if video_gen_mode == "r2v":
                req_type = "GENERATE_VIDEO_REFS"
            else:
                req_type = "GENERATE_VIDEO"

            for i, scene_obj in enumerate(scene_objects):
                req = await self.submit_request(
                    req_type=req_type,
                    scene_id=scene_obj["id"],
                    video_id=video_id,
                    project_id=project_id,
                    orientation=orientation,
                )
                video_requests.append((i, req["id"]))
                _notify("videos", f"  → Queued video for scene {i}")

            for i, req_id in video_requests:
                vid_result = await self.poll_request(
                    req_id,
                    self.video_timeout,
                    f"Video scene {i}",
                    on_progress=lambda detail: _notify("videos", detail),
                )
                scene_state = await self.get_scene(scene_objects[i]["id"])
                media_id, video_url = _scene_media(scene_state, orientation, "video")
                if not media_id and orientation == "HORIZONTAL":
                    media_id = vid_result.get("media_id")
                    video_url = video_url or vid_result.get("output_url")
                elif not video_url and orientation == "HORIZONTAL":
                    video_url = vid_result.get("output_url")
                result.scenes[i].video_url = video_url
                result.scenes[i].video_media_id = media_id
                result.scenes[i].status = "VIDEO_READY"
                if orientation == "VERTICAL" and not scene_state.get("vertical_video_media_id"):
                    result.scenes[i].status = "VIDEO_MISSING_VERTICAL_MEDIA"
                    result.scenes[i].error = "FlowKit did not expose vertical video media for this scene."
                    _notify("videos", f"  ⚠ Scene {i} has no vertical video media; not using horizontal fallback")

            if orientation == "VERTICAL" and any(not scene.video_url for scene in result.scenes):
                raise RuntimeError("FlowKit returned horizontal/fallback media but no vertical video media for at least one scene.")

            # ── 6. Upscale 4K ──
            if upscale_4k:
                _notify("upscale", "Upscaling to 4K...")
                upscale_requests = []
                for i, scene_obj in enumerate(scene_objects):
                    req = await self.submit_request(
                        req_type="UPSCALE_VIDEO",
                        scene_id=scene_obj["id"],
                        video_id=video_id,
                        project_id=project_id,
                        orientation=orientation,
                    )
                    upscale_requests.append((i, req["id"]))
                    _notify("upscale", f"  → Queued 4K for scene {i}")

                for i, req_id in upscale_requests:
                    up_result = await self.poll_request(
                        req_id,
                        self.upscale_timeout,
                        f"Upscale scene {i}",
                        on_progress=lambda detail: _notify("upscale", detail),
                    )
                    result.scenes[i].upscale_url = up_result.get("output_url")

            result.status = "COMPLETED"
            _notify("done", f"✅ Video complete! {len(scenes)} scenes.")
            return result

        except Exception as e:
            result.status = "FAILED"
            result.error = str(e)
            _notify("error", f"❌ {e}")
            raise

    # ═══════════════════════════════════════════════════════════
    # CONVENIENCE METHODS
    # ═══════════════════════════════════════════════════════════

    async def quick_video(self, prompt: str, video_prompt: str = "",
                          orientation: str = "HORIZONTAL", upscale_4k: bool = False) -> VideoResult:
        """1 scene, 1 prompt → 1 video."""
        if not video_prompt:
            video_prompt = (
                "0-3s: slow establishing shot with subtle camera movement. "
                "3-6s: dynamic tracking shot with depth of field. "
                "6-8s: push in to dramatic close-up."
            )
        return await self.generate_video(
            title="Quick Video",
            scenes=[SceneInput(prompt=prompt, video_prompt=video_prompt)],
            orientation=orientation,
            upscale_4k=upscale_4k,
        )

    async def add_scene_to_project(
        self,
        project_id: str,
        video_id: str,
        prompt: str,
        video_prompt: str = "",
        orientation: str = "HORIZONTAL",
        chain_type: str = "CONTINUATION",
        parent_scene_id: str = None,
        character_names: list[str] = None,
        generate_image: bool = True,
        generate_video: bool = True,
    ) -> SceneResult:
        """Thêm 1 scene vào project đã có."""
        existing = await self.list_scenes(video_id)
        order = len(existing)

        if not parent_scene_id and chain_type == "CONTINUATION" and existing:
            parent_scene_id = existing[-1]["id"]

        sc = await self.create_scene(
            video_id=video_id, prompt=prompt, video_prompt=video_prompt,
            display_order=order, chain_type=chain_type,
            parent_scene_id=parent_scene_id, character_names=character_names,
        )
        result = SceneResult(id=sc["id"], prompt=prompt)

        if generate_image:
            req = await self.submit_request(
                "GENERATE_IMAGE", project_id, sc["id"], video_id, orientation=orientation
            )
            img = await self.poll_request(req["id"], self.image_timeout, "Image")
            result.image_url = img.get("output_url")
            result.image_media_id = img.get("media_id")

        if generate_video:
            req = await self.submit_request(
                "GENERATE_VIDEO", project_id, sc["id"], video_id, orientation=orientation
            )
            vid = await self.poll_request(req["id"], self.video_timeout, "Video")
            result.video_url = vid.get("output_url")
            result.video_media_id = vid.get("media_id")
            result.status = "COMPLETED"

        return result

    async def regenerate_scene_image(self, project_id: str, video_id: str,
                                      scene_id: str, orientation: str = "HORIZONTAL") -> dict:
        """Regenerate a scene's image."""
        req = await self.submit_request("REGENERATE_IMAGE", project_id, scene_id, video_id, orientation=orientation)
        return await self.poll_request(req["id"], self.image_timeout, "Regen image")

    async def regenerate_scene_video(self, project_id: str, video_id: str,
                                      scene_id: str, orientation: str = "HORIZONTAL") -> dict:
        """Regenerate a scene's video."""
        req = await self.submit_request("REGENERATE_VIDEO", project_id, scene_id, video_id, orientation=orientation)
        return await self.poll_request(req["id"], self.video_timeout, "Regen video")

    async def get_full_status(self, project_id: str) -> dict:
        """Get comprehensive project status — scenes, requests, characters."""
        project = await self.get_project(project_id)
        characters = await self.get_project_characters(project_id)
        videos = await self.list_videos(project_id)
        all_scenes = []
        for v in videos:
            scenes = await self.list_scenes(v["id"])
            all_scenes.extend(scenes)
        return {
            "project": project,
            "characters": characters,
            "videos": videos,
            "scenes": all_scenes,
            "scene_count": len(all_scenes),
        }


# ═══════════════════════════════════════════════════════════════
# CLI demo
# ═══════════════════════════════════════════════════════════════

async def demo():
    """Demo: multi-scene video with characters."""
    client = FlowKitClient(
        base_url="https://flow.tanflux.tech",
        api_key="tanflux-flowkit-2026",
    )

    # Check connection
    print("Checking connection...")
    health = await client.health()
    print(f"Connected: {health}")

    # List materials
    materials = await client.list_materials()
    print(f"\nAvailable materials: {[m['id'] for m in materials]}")

    # List models
    models = await client.get_models()
    print(f"\nModels: {json.dumps(models, indent=2)}")

    # Generate a quick video
    result = await client.quick_video(
        prompt="A cyberpunk city at night with neon lights reflecting on wet streets. Flying cars pass between skyscrapers.",
        orientation="HORIZONTAL",
    )

    print(f"\n{'='*60}")
    print(f"Status: {result.status}")
    print(f"Project: {result.project_id}")
    for i, s in enumerate(result.scenes):
        print(f"Scene {i}: {s.video_url}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(demo())
