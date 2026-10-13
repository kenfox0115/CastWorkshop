"""Durable application-owned media artifacts."""
import json,os,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
MEDIA_ROOT=Path(os.environ.get("CC_MEDIA_DIR",ROOT/"data"/"media"))

def persist_artifact(conn,*,job_id,context_id,character_id,kind,data,mime_type,metadata=None):
    extension={"image":".png","video":".mp4","audio":".wav"}[kind]
    MEDIA_ROOT.mkdir(parents=True,exist_ok=True)
    name=f"{uuid.uuid4().hex}{extension}";target=MEDIA_ROOT/name
    temporary=target.with_suffix(target.suffix+".tmp")
    try:
        temporary.write_bytes(data);os.replace(temporary,target)
    finally:
        temporary.unlink(missing_ok=True)
    cur=conn.execute("""INSERT INTO artifacts(job_id,context_id,character_id,kind,relative_path,mime_type,byte_size,metadata_json)
      VALUES(?,?,?,?,?,?,?,?)""",(job_id,context_id,character_id,kind,name,mime_type,len(data),
      json.dumps(metadata) if metadata else None))
    return cur.lastrowid

def resolve_artifact(relative_path):
    root=MEDIA_ROOT.resolve();path=(root/relative_path).resolve()
    if not path.is_relative_to(root):raise ValueError("Invalid artifact path")
    return path
