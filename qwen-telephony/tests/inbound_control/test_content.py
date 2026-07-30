from pathlib import Path
import sys
import pytest
PROJECT_DIR=Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path: sys.path.insert(0,str(PROJECT_DIR))
from server.cloud_parity.store import AccessDeniedError, PlatformStore, ResourceNotFoundError
from server.inbound_control.content import ContentStore

def test_content_requires_https_publish_and_project_scope(tmp_path):
    platform=PlatformStore(tmp_path/"db.sqlite3");platform.initialize()
    first=platform.create_project(name="甲",slug="content-a",owner_id="owner-a")
    second=platform.create_project(name="乙",slug="content-b",owner_id="owner-b")
    store=ContentStore(platform);store.migrate()
    with pytest.raises(ValueError): store.create(project_id=first["id"],actor_id="owner-a",name="不安全",kind="video",source_url="http://example.com/a.mp4",description="",metadata={})
    asset=store.create(project_id=first["id"],actor_id="owner-a",name="安装视频",kind="video",source_url="https://cdn.example.com/a.mp4",description="安装",metadata={})
    with pytest.raises(ResourceNotFoundError): store.get(project_id=first["id"],asset_id=asset["id"],published_only=True)
    published=store.publish(project_id=first["id"],actor_id="owner-a",asset_id=asset["id"])
    assert published["status"]=="published"
    with pytest.raises(ResourceNotFoundError): store.get(project_id=second["id"],asset_id=asset["id"])
    platform.close()

def test_video_service_presets_are_project_scoped(tmp_path):
    platform=PlatformStore(tmp_path/"presets.sqlite3");platform.initialize()
    project=platform.create_project(name="甲",slug="video-presets",owner_id="owner")
    store=ContentStore(platform);store.migrate()
    presets=store.video_service_presets(project_id=project["id"],actor_id="owner")
    assert len(presets["avatars"])>=3 and len(presets["content"])>=3 and presets["tutorial"]["script"]
    with pytest.raises(AccessDeniedError): store.video_service_presets(project_id=project["id"],actor_id="outsider")
    platform.close()
