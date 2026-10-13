"""Local web API and static frontend."""
import io,json,os,re,sys,time
import requests
from pathlib import Path
from flask import Blueprint,Response,jsonify,request,send_file,send_from_directory
from PIL import Image,ImageOps
from scripts.model_manifest import readiness as model_readiness
from vps.artifacts import resolve_artifact
from vps.scenes import active_scene_view, scene_view
from worker.image_prompts import build_image_prompt
from worker.video_contract import dialogue_candidates, normalize_quotes
ROOT=Path(__file__).resolve().parents[1];WEB_ROOT=ROOT/"web"/"dist"
LOG_DIR=Path(os.environ.get("CC_LOG_DIR",ROOT/"logs"))
ENV_PATH=ROOT/".env"
TELEGRAM_TOKEN_PATTERN=re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{20,}$")

def _env_values():
    if not ENV_PATH.exists():return {}
    raw=ENV_PATH.read_bytes()
    try:text=raw.decode("utf-8-sig")
    except UnicodeDecodeError:text=raw.decode("cp1252")
    values={}
    for line in text.splitlines():
        key,sep,value=line.strip().partition("=")
        if sep and key and not key.startswith("#"):values[key.strip()]=value.strip().strip("\"'")
    return values

def _telegram_state():
    values=_env_values()
    token=values.get("TELEGRAM_BOT_TOKEN",os.environ.get("TELEGRAM_BOT_TOKEN",""))
    enabled=values.get("TELEGRAM_ENABLED",os.environ.get("TELEGRAM_ENABLED","1" if token else "0"))=="1"
    runtime_token=os.environ.get("TELEGRAM_BOT_TOKEN","")
    runtime_enabled=os.environ.get("TELEGRAM_ENABLED","1" if runtime_token else "0")=="1"
    return {"enabled":enabled,"configured":bool(token),"active":runtime_enabled and bool(runtime_token),
            "restart_required":enabled!=runtime_enabled or token!=runtime_token}

def _write_telegram_config(enabled,token=None,remove_token=False):
    values=_env_values();saved=values.get("TELEGRAM_BOT_TOKEN","")
    chosen="" if remove_token else ((token or "").strip() or saved)
    if enabled and not chosen:raise ValueError("Enter a Telegram bot token before enabling Telegram.")
    if chosen and not TELEGRAM_TOKEN_PATTERN.fullmatch(chosen):raise ValueError("Telegram token format is invalid.")
    lines=[]
    if ENV_PATH.exists():
        raw=ENV_PATH.read_bytes()
        try:current=raw.decode("utf-8-sig")
        except UnicodeDecodeError:current=raw.decode("cp1252")
        lines=current.splitlines()
    updates={"TELEGRAM_ENABLED":"1" if enabled else "0","TELEGRAM_BOT_TOKEN":chosen}
    found=set();result=[]
    for line in lines:
        key,sep,_=line.strip().partition("=")
        if sep and key in updates:
            if key not in found:result.append(f"{key}={updates[key]}");found.add(key)
        else:result.append(line)
    for key in ("TELEGRAM_BOT_TOKEN","TELEGRAM_ENABLED"):
        if key not in found:result.append(f"{key}={updates[key]}")
    ENV_PATH.parent.mkdir(parents=True,exist_ok=True)
    temporary=ENV_PATH.with_name(ENV_PATH.name+".tmp")
    temporary.write_text("\n".join(result).rstrip()+"\n",encoding="utf-8")
    os.replace(temporary,ENV_PATH)
    return _telegram_state()

def _heartbeat(name):
    try:age=max(0,time.time()-(LOG_DIR/f"{name}.heartbeat").stat().st_mtime)
    except OSError:return {"status":"unknown","age_seconds":None}
    return {"status":"online" if age<20 else "stale","age_seconds":round(age,1)}

def _error(code,message,status=400):return jsonify({"error":{"code":code,"message":message}}),status

def _quoted_dialogue(scene):
    normalized=normalize_quotes(scene)
    return normalized,[span.line.strip() for span in dialogue_candidates(normalized)]

def _speaker_preview(scene,char_a,char_b,guess_ambiguous=True):
    normalized,lines=_quoted_dialogue(scene);turns=[]
    for index,span in enumerate(dialogue_candidates(normalized)):
        line=span.line.strip();start=span.start;end=span.end
        prefix=normalized[max(0,start-180):start];suffix=normalized[end:end+80]
        a_before=list(re.finditer(rf'(?<!\w){re.escape(char_a["name"])}(?!\w)',prefix,re.I))
        b_before=list(re.finditer(rf'(?<!\w){re.escape(char_b["name"])}(?!\w)',prefix,re.I))
        a_after=re.match(rf'\s*,?\s*(?:says?|asks?|replies?|whispers?|shouts?)\s+{re.escape(char_a["name"])}\b',suffix,re.I)
        b_after=re.match(rf'\s*,?\s*(?:says?|asks?|replies?|whispers?|shouts?)\s+{re.escape(char_b["name"])}\b',suffix,re.I)
        if a_after or b_after:key="a" if a_after else "b"
        elif a_before or b_before:key="a" if a_before and (not b_before or a_before[-1].start()>b_before[-1].start()) else "b"
        elif guess_ambiguous:key="a" if index==0 else ("b" if turns[-1]["speaker"]=="a" else "a")
        else:key=None
        turns.append({"speaker":key,"line":line})
    return normalized,turns

RETRYABLE_JOB_TYPES={"profile_generate","edit_profile_generate","character_image_batch","promote_reference","voice_sample_batch","promote_voice","promote_scene_anchor","character_chat_intent","character_image_edit","character_video","multi_character_image","multi_character_video","comfy_image_batch"}

def _public_failure(result):
    value=(result or {}).get("text") or "The job failed without an error message."
    value=re.sub(r"(?i)(?:[A-Z]:[\\/]|/(?:Users|home)/)[^\n]+","[local path hidden]",str(value))
    return value[:1200]

def _job_view(row):
    if not row:return None
    result=json.loads(row["result"]) if row["result"] else None
    progress=json.loads(row["progress_json"] or "{}")
    cancelled=bool(result and result.get("cancelled"))
    return {"id":row["id"],"type":row["job_type"],"status":row["status"],"progress":progress,
            "failure":_public_failure(result) if row["status"]=="failed" else None,
            "cancelled":cancelled,"can_retry":row["status"]=="failed" and not cancelled and row["job_type"] in RETRYABLE_JOB_TYPES}

def create_web_blueprint(get_db,db_lock,get_setting,defaults,enqueue_job,record_message,build_identity_instruction,set_setting,build_workflow_steps,get_workflow_strategy,get_shared_character,set_shared_character):
    web=Blueprint("web",__name__)
    def context_id(conn):
        conn.execute("INSERT OR IGNORE INTO contexts(transport,external_id,display_name) VALUES('web','local','Local browser')")
        return conn.execute("SELECT id FROM contexts WHERE transport='web' AND external_id='local'").fetchone()[0]
    def selected_character(conn,cid):
        return get_shared_character(conn,0)
    def scene_state(conn,character_id):
        empty={"enabled":False,"description":"","revision":0,"anchor_artifact_id":None,"anchor_ready":False}
        if not character_id:return empty
        row=conn.execute("SELECT enabled,description,revision,anchor_artifact_id,anchor_reference_image FROM character_scenes WHERE character_id=?",(character_id,)).fetchone()
        if not row:return empty
        return {"enabled":bool(row["enabled"]),"description":row["description"],"revision":row["revision"],
                "anchor_artifact_id":row["anchor_artifact_id"],"anchor_ready":bool(row["anchor_reference_image"])}
    def scene_record(conn,character_id):
        return conn.execute("SELECT * FROM character_scenes WHERE character_id=?",(character_id,)).fetchone()
    def apply_scene(scene,latest):
        if not scene["enabled"] or not scene["description"]:return latest
        return (f"Maintain the established scene and its visible continuity: {scene['description']}\n"
                f"Latest requested action (takes precedence over the scene description): {latest}")
    def apply_anchor(latest):
        return ("Use the approved scene anchor as the visual baseline. Preserve its character identity, environment, "
                "lighting, clothing, props, and overall composition unless the latest request explicitly changes one. "
                f"Change only what the latest request requires: {latest}")
    def data(conn,cid):
        queue={r["status"]:r["count"] for r in conn.execute("SELECT status,COUNT(*) count FROM jobs WHERE context_id=? GROUP BY status",(cid,))}
        active_job=conn.execute("SELECT id FROM jobs WHERE context_id=? AND status IN ('queued','dispatched','interrupted') ORDER BY id LIMIT 1",(cid,)).fetchone()
        active_job_detail=conn.execute("SELECT id,job_type,status,result,progress_json FROM jobs WHERE context_id=? AND status IN ('queued','dispatched','interrupted') ORDER BY id LIMIT 1",(cid,)).fetchone()
        latest_job=conn.execute("SELECT id,job_type,status,result,progress_json FROM jobs WHERE context_id=? ORDER BY id DESC LIMIT 1",(cid,)).fetchone()
        recent_job=_job_view(latest_job) if latest_job and latest_job["status"]=="failed" else None
        creation=conn.execute("SELECT character_id,state FROM sessions WHERE context_id=? AND state!='active' ORDER BY id DESC LIMIT 1",(cid,)).fetchone()
        active_character=get_shared_character(conn,0);active_id=active_character["id"] if active_character else None
        aspect=get_setting(conn,"aspect_mode") or defaults["aspect_mode"]
        reusable_active=active_scene_view(conn,aspect)
        rows=conn.execute("""SELECT c.id,c.name,c.base_image_path IS NOT NULL image_ready,c.voice_sample_path IS NOT NULL voice_ready,
          EXISTS(SELECT 1 FROM sessions s WHERE s.character_id=c.id AND s.state='active') ready
          FROM characters c ORDER BY c.created_at DESC,c.id DESC LIMIT 100""").fetchall()
        messages=[]
        if active_id:
            message_rows=conn.execute("""SELECT m.id,m.role,m.kind,m.content,m.created_at,COALESCE(ctx.transport,'legacy') source FROM messages m
              LEFT JOIN contexts ctx ON ctx.id=m.context_id
              WHERE (m.character_id=? OR (m.character_id IS NULL AND EXISTS(
                       SELECT 1 FROM message_artifacts sma JOIN artifacts sa ON sa.id=sma.artifact_id
                       JOIN jobs sj ON sj.id=sa.job_id WHERE sma.message_id=m.id
                         AND json_extract(sj.payload,'$.scene_anchor_candidate')=1
                          AND json_extract(sj.payload,'$.scene_id')=?)))
                AND (role!='assistant' OR kind NOT IN ('image','video')
                     OR EXISTS(SELECT 1 FROM message_artifacts ma WHERE ma.message_id=m.id))
              ORDER BY m.id DESC LIMIT 50""",(active_id,reusable_active["id"] if reusable_active else -1)).fetchall()[::-1]
            for row in message_rows:
                item=dict(row)
                item["artifacts"]=[dict(a) for a in conn.execute("""SELECT a.id,a.kind,a.mime_type,a.byte_size,j.job_type
                  FROM artifacts a JOIN message_artifacts ma ON ma.artifact_id=a.id LEFT JOIN jobs j ON j.id=a.job_id
                  WHERE ma.message_id=? ORDER BY ma.display_order""",(row["id"],))]
                messages.append(item)
        reusable_scenes=[scene_view(conn,row["id"],aspect) for row in conn.execute("SELECT id FROM scenes ORDER BY updated_at DESC,id DESC")]
        return {"services":{"vps":{"status":"online"},"worker":_heartbeat("worker")},
          "queue":{"queued":queue.get("queued",0),"running":queue.get("dispatched",0),"failed":queue.get("failed",0),"completed":queue.get("done",0)},
          "settings":{"content_mode":get_setting(conn,"content_mode") or defaults["content_mode"],"aspect_mode":get_setting(conn,"aspect_mode") or defaults["aspect_mode"],"character_videos":get_setting(conn,"character_videos")!="off","model":get_setting(conn,"current_model") or os.environ.get("OLLAMA_MODEL","qwen3-14b-64k"),"debug_mode":get_setting(conn,"web_debug_mode")=="on","endpoint_ollama":get_setting(conn,"endpoint_ollama") or "http://127.0.0.1:11434","endpoint_comfyui":get_setting(conn,"endpoint_comfyui") or "http://127.0.0.1:8188","telegram":_telegram_state(),"workflows":[{"key":step["key"],"label":step["label"],"selected":get_workflow_strategy(conn,step["key"])["key"],"strategies":step["strategies"]} for step in build_workflow_steps()]},
          "active_character_id":active_id,
          "scene":scene_state(conn,active_id),
          "scenes":reusable_scenes,"active_scene":reusable_active,
          "active_job_id":active_job[0] if active_job else None,
          "active_job":_job_view(active_job_detail),
          "recent_job":recent_job,
          "active_creation":{"character_id":creation["character_id"],"stage":creation["state"]} if creation else None,"messages":messages,
          "characters":[{"id":r["id"],"name":r["name"] or "Unnamed character","state":"ready" if r["ready"] else "setup","image_ready":bool(r["image_ready"]),"voice_ready":bool(r["voice_ready"])} for r in rows]}
    def snapshot():
        with db_lock,get_db() as conn:return data(conn,context_id(conn))
    def text(body):
        value=body.get("content")
        if not isinstance(value,str) or not value.strip():return None
        return value.strip() if len(value)<=4000 else None
    def active_heavy(conn,cid):
        return conn.execute("SELECT id FROM jobs WHERE context_id=? AND weight='heavy' AND status IN ('queued','dispatched') LIMIT 1",(cid,)).fetchone()
    def work_conflict(conn,cid):
        return conn.execute("SELECT 1 FROM jobs WHERE context_id=? AND status IN ('queued','dispatched') LIMIT 1",(cid,)).fetchone() or conn.execute("SELECT 1 FROM sessions WHERE context_id=? AND state!='active' LIMIT 1",(cid,)).fetchone()
    @web.after_request
    def headers(response):
        if response.content_type.startswith("application/json"):response.headers["Cache-Control"]="no-store"
        return response
    @web.get("/api/v1/health")
    def health():
        d=snapshot();return jsonify({"status":"ok","services":d["services"],"queue":d["queue"]})
    @web.get("/api/v1/characters")
    def characters():return jsonify({"characters":snapshot()["characters"]})
    @web.post("/api/v1/characters")
    def create_character():
        body=request.get_json(silent=True) or {}
        values={key:body.get(key) for key in ("name","concept","personality_notes")}
        if any(not isinstance(value,str) or not value.strip() or len(value)>6000 for value in values.values()):
            return _error("validation_error","Name, look, and personality are required.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            if conn.execute("SELECT 1 FROM sessions WHERE context_id=? AND state!='active'",(cid,)).fetchone():
                return _error("state_conflict","Finish the current character setup first.",409)
            cur=conn.execute("INSERT INTO characters(name,concept,personality_notes) VALUES(?,?,?)",tuple(values[k].strip() for k in ("name","concept","personality_notes")))
            character_id=cur.lastrowid
            conn.execute("INSERT INTO sessions(character_id,chat_id,context_id,state) VALUES(?,0,?,'profile_generating')",(character_id,cid))
            conn.execute("""INSERT INTO context_selections(context_id,character_id,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
              ON CONFLICT(context_id) DO UPDATE SET character_id=excluded.character_id,updated_at=CURRENT_TIMESTAMP""",(cid,character_id))
            set_shared_character(conn,character_id)
            job_id=enqueue_job(conn,0,"profile_generate","light",{"character_id":character_id,"personality_notes":values["personality_notes"].strip()},context_id=cid)
            return jsonify({"id":character_id,"job_id":job_id,"stage":"profile_generating"}),201
    @web.get("/api/v1/characters/<int:character_id>/creation-state")
    def creation_state(character_id):
        with db_lock,get_db() as conn:
            cid=context_id(conn);char=conn.execute("SELECT id,name,base_image_path,voice_sample_path FROM characters WHERE id=?",(character_id,)).fetchone()
            if not char:return _error("not_found","Character not found.",404)
            session=conn.execute("SELECT state,pending_data FROM sessions WHERE character_id=? AND context_id=? ORDER BY id DESC LIMIT 1",(character_id,cid)).fetchone()
            stage=session["state"] if session else ("ready" if char["base_image_path"] and char["voice_sample_path"] else "incomplete")
            if stage=="active":stage="ready"
            job=conn.execute("SELECT id,status,result FROM jobs WHERE context_id=? AND json_extract(payload,'$.character_id')=? ORDER BY id DESC LIMIT 1",(cid,character_id)).fetchone()
            result=json.loads(job["result"]) if job and job["result"] else {}
            pending=json.loads(session["pending_data"] or "[]") if session else []
            return jsonify({"character_id":character_id,"name":char["name"],"stage":stage,"job_id":job["id"] if job else None,"job_status":job["status"] if job else None,"candidate_artifact_ids":result.get("candidate_artifact_ids",[]),"candidate_count":len(pending)})
    @web.post("/api/v1/characters/<int:character_id>/look-selection")
    def select_look(character_id):
        body=request.get_json(silent=True) or {};choice=body.get("choice")
        with db_lock,get_db() as conn:
            cid=context_id(conn);session=conn.execute("SELECT * FROM sessions WHERE character_id=? AND context_id=? AND state='awaiting_choice'",(character_id,cid)).fetchone()
            if not session:return _error("state_conflict","Look candidates are not awaiting selection.",409)
            pending=json.loads(session["pending_data"] or "[]")
            if not isinstance(choice,int) or not 0<=choice<len(pending):return _error("validation_error","Choose a valid look candidate.",422)
            conn.execute("UPDATE sessions SET state='promoting_reference',updated_at=CURRENT_TIMESTAMP WHERE id=?",(session["id"],))
            job_id=enqueue_job(conn,0,"promote_reference","heavy",{"character_id":character_id,"filename":pending[choice]},context_id=cid)
            return jsonify({"job_id":job_id,"status":"queued"}),202
    @web.post("/api/v1/characters/<int:character_id>/voice-selection")
    def select_voice(character_id):
        body=request.get_json(silent=True) or {};choice=body.get("choice")
        with db_lock,get_db() as conn:
            cid=context_id(conn);session=conn.execute("SELECT * FROM sessions WHERE character_id=? AND context_id=? AND state='awaiting_voice_choice'",(character_id,cid)).fetchone()
            if not session:return _error("state_conflict","Voice candidates are not awaiting selection.",409)
            pending=json.loads(session["pending_data"] or "[]")
            if not isinstance(choice,int) or not 0<=choice<len(pending):return _error("validation_error","Choose a valid voice candidate.",422)
            selected=pending[choice];conn.execute("UPDATE sessions SET state='promoting_voice',updated_at=CURRENT_TIMESTAMP WHERE id=?",(session["id"],))
            job_id=enqueue_job(conn,0,"promote_voice","heavy",{"character_id":character_id,"filename":selected["filename"],"subfolder":selected.get("subfolder","")},context_id=cid)
            return jsonify({"job_id":job_id,"status":"queued"}),202
    @web.post("/api/v1/characters/<int:character_id>/look-candidates")
    def reroll_looks(character_id):
        with db_lock,get_db() as conn:
            cid=context_id(conn);session=conn.execute("SELECT * FROM sessions WHERE character_id=? AND context_id=? AND state='awaiting_choice'",(character_id,cid)).fetchone()
            char=conn.execute("SELECT concept, personality_notes FROM characters WHERE id=?",(character_id,)).fetchone()
            if not session or not char:return _error("state_conflict","Look candidates are not awaiting selection.",409)
            conn.execute("UPDATE sessions SET state='image_queued',pending_data=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(session["id"],))
            job_id=enqueue_job(conn,0,"character_image_batch","heavy",{"character_id":character_id,"prompt":char["concept"],"personality_notes":char["personality_notes"]},context_id=cid)
            return jsonify({"job_id":job_id,"status":"queued"}),202
    @web.post("/api/v1/characters/<int:character_id>/voice-candidates")
    def reroll_voices(character_id):
        with db_lock,get_db() as conn:
            cid=context_id(conn);session=conn.execute("SELECT * FROM sessions WHERE character_id=? AND context_id=? AND state='awaiting_voice_choice'",(character_id,cid)).fetchone()
            previous=conn.execute("SELECT payload FROM jobs WHERE context_id=? AND job_type='voice_sample_batch' AND json_extract(payload,'$.character_id')=? ORDER BY id DESC LIMIT 1",(cid,character_id)).fetchone()
            if not session or not previous:return _error("state_conflict","Voice candidates are not awaiting selection.",409)
            payload=json.loads(previous["payload"]);payload.pop("settings_snapshot",None);payload.pop("user_message_id",None)
            conn.execute("UPDATE sessions SET state='voice_generating',pending_data=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(session["id"],))
            job_id=enqueue_job(conn,0,"voice_sample_batch","heavy",payload,context_id=cid)
            return jsonify({"job_id":job_id,"status":"queued"}),202
    @web.get("/api/v1/bootstrap")
    def bootstrap():return jsonify(snapshot())
    @web.get("/api/v1/scenes")
    def reusable_scenes():
        with db_lock,get_db() as conn:
            aspect=get_setting(conn,"aspect_mode") or defaults["aspect_mode"]
            rows=[scene_view(conn,row["id"],aspect) for row in conn.execute("SELECT id FROM scenes ORDER BY updated_at DESC,id DESC")]
            return jsonify({"scenes":rows,"active_scene":active_scene_view(conn,aspect)})
    @web.post("/api/v1/scenes")
    def create_reusable_scene():
        body=request.get_json(silent=True) or {};name=body.get("name");description=body.get("description")
        if not isinstance(name,str) or not name.strip() or len(name.strip())>120:
            return _error("validation_error","Give the scene a name of 120 characters or fewer.",422)
        if not isinstance(description,str) or not description.strip() or len(description.strip())>2000:
            return _error("validation_error","Describe the scene in 2000 characters or fewer.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            cur=conn.execute("INSERT INTO scenes(name,description) VALUES(?,?)",(name.strip(),description.strip()))
            set_setting(conn,"active_scene_id",str(cur.lastrowid))
            aspect=get_setting(conn,"aspect_mode") or defaults["aspect_mode"]
            return jsonify(scene_view(conn,cur.lastrowid,aspect)),201
    @web.put("/api/v1/scenes/<int:scene_id>")
    def update_reusable_scene(scene_id):
        body=request.get_json(silent=True) or {};name=body.get("name");description=body.get("description")
        if not isinstance(name,str) or not name.strip() or len(name.strip())>120 or not isinstance(description,str) or not description.strip() or len(description.strip())>2000:
            return _error("validation_error","A scene name and description are required.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            old=conn.execute("SELECT description FROM scenes WHERE id=?",(scene_id,)).fetchone()
            if not old:return _error("not_found","Scene not found.",404)
            if old["description"]!=description.strip():conn.execute("DELETE FROM scene_assets WHERE scene_id=?",(scene_id,))
            conn.execute("UPDATE scenes SET name=?,description=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=?",(name.strip(),description.strip(),scene_id))
            aspect=get_setting(conn,"aspect_mode") or defaults["aspect_mode"]
            return jsonify(scene_view(conn,scene_id,aspect))
    @web.put("/api/v1/active-scene")
    def select_reusable_scene():
        body=request.get_json(silent=True) or {};scene_id=body.get("scene_id")
        if scene_id is not None and not isinstance(scene_id,int):return _error("validation_error","Choose a valid scene or no scene.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            if scene_id is not None and not conn.execute("SELECT 1 FROM scenes WHERE id=?",(scene_id,)).fetchone():return _error("not_found","Scene not found.",404)
            if scene_id is None:conn.execute("DELETE FROM settings WHERE key='active_scene_id'")
            else:set_setting(conn,"active_scene_id",str(scene_id))
            aspect=get_setting(conn,"aspect_mode") or defaults["aspect_mode"]
            return jsonify({"active_scene":active_scene_view(conn,aspect)})
    @web.delete("/api/v1/scenes/<int:scene_id>")
    def delete_reusable_scene(scene_id):
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            active=active_scene_view(conn)
            if active and active["id"]==scene_id:conn.execute("DELETE FROM settings WHERE key='active_scene_id'")
            cur=conn.execute("DELETE FROM scenes WHERE id=?",(scene_id,))
            if not cur.rowcount:return _error("not_found","Scene not found.",404)
            return ("",204)
    @web.post("/api/v1/scenes/<int:scene_id>/anchor-candidate")
    def generate_reusable_scene_anchor(scene_id):
        body=request.get_json(silent=True) or {};aspect=body.get("aspect_mode")
        if aspect not in ("vertical","cinematic"):return _error("validation_error","Choose vertical or cinematic.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            scene=scene_view(conn,scene_id,aspect)
            if not scene:return _error("not_found","Scene not found.",404)
            prompt=("Create a canonical, photorealistic establishing image of this environment: "+scene["description"]+". "
                    "The location must be completely unoccupied: no people, characters, faces, bodies, silhouettes, "
                    "reflections of people, or human figures. Preserve clear open staging areas where characters can be added later. "
                    f"Compose specifically for a {aspect} final frame.")
            job_id=enqueue_job(conn,0,"comfy_image_batch","heavy",{
                "prompt":prompt,"scene_anchor_candidate":True,"scene_id":scene_id,"scene_revision":scene["revision"],
                "scene_aspect_mode":aspect,"caption":f"{scene['name']} - {aspect} anchor candidate",
            },priority=5,context_id=cid)
            return jsonify({"job_id":job_id,"status":"queued"}),202
    @web.post("/api/v1/scenes/<int:scene_id>/anchor")
    def approve_reusable_scene_anchor(scene_id):
        body=request.get_json(silent=True) or {};artifact_id=body.get("artifact_id");aspect=body.get("aspect_mode")
        if not isinstance(artifact_id,int) or aspect not in ("vertical","cinematic"):
            return _error("validation_error","Choose a generated anchor and its aspect.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            if not scene_view(conn,scene_id,aspect):return _error("not_found","Scene not found.",404)
            artifact=conn.execute("""SELECT a.id FROM artifacts a JOIN jobs j ON j.id=a.job_id
              WHERE a.id=? AND a.kind='image' AND json_extract(j.payload,'$.scene_id')=?
                AND json_extract(j.payload,'$.scene_aspect_mode')=?""",(artifact_id,scene_id,aspect)).fetchone()
            if not artifact:return _error("not_found","That image is not an anchor candidate for this scene and aspect.",404)
            job_id=enqueue_job(conn,0,"promote_scene_anchor","light",{
                "scene_id":scene_id,"aspect_mode":aspect,"artifact_id":artifact_id,
                "artifact_url":f"/api/v1/artifacts/{artifact_id}"},context_id=cid)
            return jsonify({"job_id":job_id,"status":"queued"}),202
    @web.delete("/api/v1/scenes/<int:scene_id>/anchor/<aspect>")
    def clear_reusable_scene_anchor(scene_id,aspect):
        if aspect not in ("vertical","cinematic"):return _error("validation_error","Choose vertical or cinematic.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            conn.execute("DELETE FROM scene_assets WHERE scene_id=? AND aspect_mode=?",(scene_id,aspect))
            return ("",204)
    @web.get("/api/v1/scene")
    def get_scene():
        with db_lock,get_db() as conn:
            cid=context_id(conn);selected=selected_character(conn,cid)
            if not selected:return _error("state_conflict","Select a character first.",409)
            return jsonify(scene_state(conn,selected["id"]))
    @web.put("/api/v1/scene")
    def put_scene():
        body=request.get_json(silent=True) or {};enabled=body.get("enabled");description=body.get("description")
        if not isinstance(enabled,bool) or not isinstance(description,str) or len(description.strip())>2000:
            return _error("validation_error","Scene state requires an enabled flag and a description of 2000 characters or fewer.",422)
        description=description.strip()
        if enabled and not description:return _error("validation_error","Describe the scene before enabling continuity.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn);selected=selected_character(conn,cid)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            if not selected:return _error("state_conflict","Select a character first.",409)
            conn.execute("""INSERT INTO character_scenes(character_id,enabled,description) VALUES(?,?,?)
              ON CONFLICT(character_id) DO UPDATE SET enabled=excluded.enabled,description=excluded.description,
              anchor_artifact_id=CASE WHEN character_scenes.description<>excluded.description THEN NULL ELSE character_scenes.anchor_artifact_id END,
              anchor_reference_image=CASE WHEN character_scenes.description<>excluded.description THEN NULL ELSE character_scenes.anchor_reference_image END,
              revision=character_scenes.revision+1,updated_at=CURRENT_TIMESTAMP""",(selected["id"],int(enabled),description))
            return jsonify(scene_state(conn,selected["id"]))
    @web.delete("/api/v1/scene")
    def delete_scene():
        with db_lock,get_db() as conn:
            cid=context_id(conn);selected=selected_character(conn,cid)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            if not selected:return _error("state_conflict","Select a character first.",409)
            conn.execute("DELETE FROM character_scenes WHERE character_id=?",(selected["id"],))
            return ("",204)
    @web.post("/api/v1/scene/anchor-candidate")
    def generate_scene_anchor():
        with db_lock,get_db() as conn:
            cid=context_id(conn);selected=selected_character(conn,cid)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            if not selected:return _error("state_conflict","Select a character first.",409)
            scene=scene_record(conn,selected["id"])
            if not scene or not scene["enabled"] or not scene["description"].strip():
                return _error("state_conflict","Save and enable a scene before generating its anchor.",409)
            if not selected["base_image_path"]:return _error("state_conflict","The selected character needs a reference image.",409)
            instruction=build_identity_instruction(selected["concept"],
                "Create the canonical scene anchor. Preserve the reference character's identity. "
                "Establish this exact environment, lighting, clothing, props, and composition: "+scene["description"])
            job_id=enqueue_job(conn,0,"character_image_edit","heavy",{
                "character_id":selected["id"],"prompt":instruction,"reference_image":selected["base_image_path"],
                "caption":"Scene anchor candidate. Approve this image to use it for continuity.",
                "scene_anchor_candidate":True,"scene_context":scene["description"],"scene_revision":scene["revision"]},priority=5,context_id=cid)
            return jsonify({"job_id":job_id,"status":"queued"}),202
    @web.post("/api/v1/scene/anchor")
    def approve_scene_anchor():
        body=request.get_json(silent=True) or {};artifact_id=body.get("artifact_id")
        if not isinstance(artifact_id,int):return _error("validation_error","Choose an image to use as the scene anchor.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn);selected=selected_character(conn,cid)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            if not selected:return _error("state_conflict","Select a character first.",409)
            scene=scene_record(conn,selected["id"])
            if not scene or not scene["enabled"]:return _error("state_conflict","Enable scene continuity before choosing an anchor.",409)
            artifact=conn.execute("SELECT id,relative_path FROM artifacts WHERE id=? AND character_id=? AND kind='image'",(artifact_id,selected["id"])).fetchone()
            if not artifact:return _error("not_found","That image does not belong to the selected character.",404)
            job_id=enqueue_job(conn,0,"promote_scene_anchor","light",{
                "character_id":selected["id"],"artifact_id":artifact_id,"artifact_url":f"/api/v1/artifacts/{artifact_id}"},context_id=cid)
            return jsonify({"job_id":job_id,"status":"queued"}),202
    @web.delete("/api/v1/scene/anchor")
    def clear_scene_anchor():
        with db_lock,get_db() as conn:
            cid=context_id(conn);selected=selected_character(conn,cid)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            if not selected:return _error("state_conflict","Select a character first.",409)
            conn.execute("UPDATE character_scenes SET anchor_artifact_id=NULL,anchor_reference_image=NULL,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE character_id=?",(selected["id"],))
            return jsonify(scene_state(conn,selected["id"]))
    @web.get("/api/v1/models/readiness")
    def models_readiness():
        with db_lock,get_db() as conn:
            ollama=get_setting(conn,"endpoint_ollama") or "http://127.0.0.1:11434"
            comfy=get_setting(conn,"endpoint_comfyui") or "http://127.0.0.1:8188"
            model=get_setting(conn,"current_model") or os.environ.get("OLLAMA_MODEL","qwen3-14b-64k")
        return jsonify(model_readiness(ollama,comfy,ollama_model=model))
    @web.put("/api/v1/active-character")
    def select_character():
        body=request.get_json(silent=True) or {};character_id=body.get("character_id")
        if not isinstance(character_id,int):return _error("validation_error","character_id must be an integer.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            if not conn.execute("SELECT 1 FROM characters WHERE id=?",(character_id,)).fetchone():return _error("not_found","Character not found.",404)
            conn.execute("""INSERT INTO context_selections(context_id,character_id,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
              ON CONFLICT(context_id) DO UPDATE SET character_id=excluded.character_id,updated_at=CURRENT_TIMESTAMP""",(cid,character_id))
            set_shared_character(conn,character_id)
            return jsonify(data(conn,cid))
    @web.post("/api/v1/messages")
    def send_message():
        body=request.get_json(silent=True) or {};message=text(body)
        if message is None:return _error("validation_error","Message text is required and must be 4000 characters or fewer.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn);selected=selected_character(conn,cid)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            if not selected:return _error("state_conflict","Select a character before sending a message.",409)
            record_message(conn,selected["id"],0,"user",message,kind="chat",context_id=cid)
            scene=scene_state(conn,selected["id"]);stored=scene_record(conn,selected["id"])
            job_id=enqueue_job(conn,0,"character_chat_intent","light",{"character_id":selected["id"],"system_prompt":selected["system_prompt"],"concept":selected["concept"],"message":message,"allow_media":True,"scene_context":scene["description"] if scene["enabled"] else "","scene_revision":scene["revision"],"scene_reference_image":stored["anchor_reference_image"] if stored and scene["enabled"] else None},context_id=cid)
            return jsonify({"job_id":job_id,"status":"queued"}),202
    def request_media(kind):
        body=request.get_json(silent=True) or {};description=text(body)
        if description is None:return _error("validation_error","A description is required and must be 4000 characters or fewer.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn);selected=selected_character(conn,cid)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            if not selected:return _error("state_conflict","Select a character first.",409)
            if not selected["base_image_path"]:return _error("state_conflict","The selected character needs a reference image.",409)
            if active_heavy(conn,cid):return _error("job_conflict","A media job is already queued or running.",409)
            user_message_id=record_message(conn,selected["id"],0,"user",description,kind=kind,context_id=cid)
            aspect=get_setting(conn,"aspect_mode") or defaults["aspect_mode"]
            reusable=active_scene_view(conn,aspect);asset=reusable.get("active_asset") if reusable else None
            effective=description;reference=selected["base_image_path"]
            if reusable:
                effective=(f"Maintain this reusable environment: {reusable['description']}\nLatest requested action: {description}")
                scene_context=reusable["description"];scene_revision=reusable["revision"];scene_id=reusable["id"]
                scene_reference=asset["reference_image"] if asset and asset.get("ready") else None
            else:
                legacy=scene_state(conn,selected["id"]);stored=scene_record(conn,selected["id"]);effective=apply_scene(legacy,description)
                reference=(stored["anchor_reference_image"] if stored and legacy["enabled"] and stored["anchor_reference_image"] else reference)
                if reference!=selected["base_image_path"]:effective=apply_anchor(effective)
                scene_context=legacy["description"] if legacy["enabled"] else "";scene_revision=legacy["revision"];scene_id=None;scene_reference=None
            instruction=build_image_prompt(effective, ((2,"the character"),), 1) if scene_reference else build_identity_instruction(selected["concept"],effective)
            if kind=="image":
                payload={"character_id":selected["id"],"prompt":instruction,"reference_image":reference,"user_message_id":user_message_id,"scene_context":scene_context,"scene_id":scene_id,"scene_revision":scene_revision,"scene_aspect_mode":aspect,"scene_reference_image":scene_reference,"using_scene_anchor":bool(scene_reference or reference!=selected["base_image_path"])}
                job_type="character_image_edit";priority=5
            else:
                payload={"character_id":selected["id"],"scene_prompt":instruction,"motion_prompt":effective,"user_message_id":user_message_id,"scene_context":scene_context,"scene_id":scene_id,"scene_revision":scene_revision,"scene_aspect_mode":aspect,"scene_reference_image":scene_reference,
                  "reference_image":reference,"concept":selected["concept"],"using_scene_anchor":bool(scene_reference or reference!=selected["base_image_path"]),
                  "voice_audio":selected["voice_sample_path"] or None,"character_name":selected["name"]}
                job_type="character_video";priority=10
            job_id=enqueue_job(conn,0,job_type,"heavy",payload,priority=priority,context_id=cid)
            return jsonify({"job_id":job_id,"status":"queued"}),202
    @web.post("/api/v1/media/image")
    def image():return request_media("image")
    @web.post("/api/v1/media/video")
    def video():return request_media("video")
    @web.post("/api/v1/artifacts/<int:artifact_id>/continue")
    def continue_video(artifact_id):
        body=request.get_json(silent=True) or {};description=text(body)
        if description is None:return _error("validation_error","Describe what happens in the next clip using 4000 characters or fewer.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn);selected=selected_character(conn,cid)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            source=conn.execute("""SELECT a.id,a.relative_path,a.character_id,a.job_id,j.job_type,j.payload
              FROM artifacts a JOIN jobs j ON j.id=a.job_id
              WHERE a.id=? AND a.kind='video' AND a.character_id=?
                AND EXISTS(SELECT 1 FROM message_artifacts ma WHERE ma.artifact_id=a.id)""",
              (artifact_id,selected["id"] if selected else None)).fetchone()
            if not source:return _error("not_found","That video is not available in this conversation.",404)
            if source["job_type"] not in ("character_video","multi_character_video"):
                return _error("state_conflict","That video cannot be continued.",409)
            original=json.loads(source["payload"]);character_id=source["character_id"] or original.get("character_id")
            confirmed_turns=None
            if source["job_type"]=="multi_character_video":
                char_a=original.get("char_a") or {};char_b=original.get("char_b") or {}
                normalized,preview_turns=_speaker_preview(description,char_a,char_b,guess_ambiguous=False)
                if body.get("confirmed") is not True:
                    return jsonify({"requires_speaker_confirmation":True,"scene_text":normalized,
                        "characters":{"a":{"name":char_a.get("name","")},"b":{"name":char_b.get("name","")}},
                        "dialogue_turns":preview_turns})
                supplied=body.get("dialogue_turns")
                if not isinstance(supplied,list) or any(
                    not isinstance(turn,dict) or turn.get("speaker") not in ("a","b")
                    or not isinstance(turn.get("line"),str) or not turn["line"].strip()
                    or len(turn["line"])>240 for turn in supplied):
                    return _error("validation_error","Every dialogue line must have a confirmed speaker.",422)
                if [turn["line"].strip() for turn in supplied] != [turn["line"] for turn in preview_turns]:
                    return _error("validation_error","Confirmed dialogue must match the quoted continuation dialogue in order.",422)
                confirmed_turns=[{"speaker":turn["speaker"],"line":turn["line"].strip()} for turn in supplied]
            user_message_id=record_message(conn,character_id,0,"user",description,kind="video",context_id=cid)
            payload=dict(original)
            payload.update({
                "character_id":character_id,"user_message_id":user_message_id,
                "continuation_artifact_id":artifact_id,
                "continuation_parent_job_id":source["job_id"],
                "continuation_relative_path":source["relative_path"],
                "continuation_request":description,
                "caption":"Continued video.",
            })
            payload.pop("retry_of_job_id",None)
            if source["job_type"]=="character_video":
                payload.update({"motion_prompt":description,"scene_prompt":description})
                payload.pop("spoken_line",None);payload.pop("video_line",None)
            else:
                char_a=original.get("char_a") or {};char_b=original.get("char_b") or {}
                normalized=normalize_quotes(description);turns=confirmed_turns
                first_a=next((turn["line"] for turn in turns if turn["speaker"]=="a"),None)
                first_b=next((turn["line"] for turn in turns if turn["speaker"]=="b"),None)
                payload.update({"scene_text":normalized,"interaction_text":normalized,
                                "dialogue_turns":turns,"spoken_line_a":first_a,"spoken_line_b":first_b})
            job_id=enqueue_job(conn,0,source["job_type"],"heavy",payload,priority=10,context_id=cid)
            return jsonify({"job_id":job_id,"status":"queued","continuation_of_artifact_id":artifact_id}),202
    @web.post("/api/v1/media/multi-image")
    def multi_image():
        body=request.get_json(silent=True) or {};description=text(body)
        char_a_id=body.get("character_a_id");char_b_id=body.get("character_b_id")
        if description is None:return _error("validation_error","A scene description is required and must be 4000 characters or fewer.",422)
        if not isinstance(char_a_id,int) or not isinstance(char_b_id,int) or char_a_id==char_b_id:
            return _error("validation_error","Choose two different characters.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            chars={r["id"]:r for r in conn.execute("SELECT * FROM characters WHERE id IN (?,?)",(char_a_id,char_b_id))}
            if len(chars)!=2:return _error("not_found","One of the selected characters was not found.",404)
            char_a,char_b=chars[char_a_id],chars[char_b_id]
            if not char_a["base_image_path"] or not char_b["base_image_path"]:
                return _error("state_conflict","Both characters need reference images.",409)
            conn.execute("""INSERT INTO context_selections(context_id,character_id,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
              ON CONFLICT(context_id) DO UPDATE SET character_id=excluded.character_id,updated_at=CURRENT_TIMESTAMP""",(cid,char_a_id))
            set_shared_character(conn,char_a_id)
            user_message_id=record_message(conn,char_a_id,0,"user",description,kind="image",context_id=cid)
            aspect=get_setting(conn,"aspect_mode") or defaults["aspect_mode"];reusable=active_scene_view(conn,aspect);asset=reusable.get("active_asset") if reusable else None
            payload={"character_id":char_a_id,"user_message_id":user_message_id,"char_a":{"name":char_a["name"],"concept":char_a["concept"],"base_image":char_a["base_image_path"]},"char_b":{"name":char_b["name"],"concept":char_b["concept"],"base_image":char_b["base_image_path"]},"scene_text":description,"interaction_text":description,"caption":f"{char_a['name']} and {char_b['name']}","scene_id":reusable["id"] if reusable else None,"scene_context":reusable["description"] if reusable else "","scene_revision":reusable["revision"] if reusable else 0,"scene_aspect_mode":aspect,"scene_reference_image":asset["reference_image"] if asset and asset.get("ready") else None}
            job_id=enqueue_job(conn,0,"multi_character_image","heavy",payload,context_id=cid)
            return jsonify({"job_id":job_id,"status":"queued"}),202
    def multi_video_characters(conn,body):
        char_a_id=body.get("character_a_id");char_b_id=body.get("character_b_id")
        if not isinstance(char_a_id,int) or not isinstance(char_b_id,int) or char_a_id==char_b_id:return None,None,_error("validation_error","Choose two different characters.",422)
        chars={r["id"]:r for r in conn.execute("SELECT * FROM characters WHERE id IN (?,?)",(char_a_id,char_b_id))}
        if len(chars)!=2:return None,None,_error("not_found","One of the selected characters was not found.",404)
        char_a,char_b=chars[char_a_id],chars[char_b_id]
        if not char_a["base_image_path"] or not char_b["base_image_path"]:return None,None,_error("state_conflict","Both characters need reference images.",409)
        if not char_a["voice_sample_path"] or not char_b["voice_sample_path"]:return None,None,_error("state_conflict","Both characters need voice samples.",409)
        return char_a,char_b,None
    @web.post("/api/v1/media/multi-video/preview")
    def multi_video_preview():
        body=request.get_json(silent=True) or {};description=text(body)
        if description is None:return _error("validation_error","A scene description is required and must be 4000 characters or fewer.",422)
        with db_lock,get_db() as conn:
            context_id(conn);char_a,char_b,error=multi_video_characters(conn,body)
            if error:return error
            normalized,turns=_speaker_preview(description,char_a,char_b)
            return jsonify({"scene_text":normalized,"characters":{"a":{"id":char_a["id"],"name":char_a["name"]},"b":{"id":char_b["id"],"name":char_b["name"]}},"dialogue_turns":turns})
    @web.post("/api/v1/media/multi-video")
    def multi_video():
        body=request.get_json(silent=True) or {};description=text(body);turns=body.get("dialogue_turns")
        if description is None:return _error("validation_error","A scene description is required and must be 4000 characters or fewer.",422)
        if body.get("confirmed") is not True:return _error("confirmation_required","Review and confirm the speakers before generating the video.",409)
        if not isinstance(turns,list) or any(not isinstance(turn,dict) or turn.get("speaker") not in ("a","b") or not isinstance(turn.get("line"),str) or not turn["line"].strip() or len(turn["line"])>240 for turn in turns):
            return _error("validation_error","Every dialogue line must have a confirmed speaker.",422)
        normalized,quoted=_quoted_dialogue(description)
        if [turn["line"].strip() for turn in turns]!=quoted:return _error("validation_error","Confirmed dialogue must match the quoted scene dialogue in order.",422)
        confirmed=[{"speaker":turn["speaker"],"line":turn["line"].strip()} for turn in turns]
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            char_a,char_b,error=multi_video_characters(conn,body)
            if error:return error
            conn.execute("""INSERT INTO context_selections(context_id,character_id,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
              ON CONFLICT(context_id) DO UPDATE SET character_id=excluded.character_id,updated_at=CURRENT_TIMESTAMP""",(cid,char_a["id"]))
            set_shared_character(conn,char_a["id"])
            user_message_id=record_message(conn,char_a["id"],0,"user",normalized,kind="video",context_id=cid)
            aspect=get_setting(conn,"aspect_mode") or defaults["aspect_mode"];reusable=active_scene_view(conn,aspect);asset=reusable.get("active_asset") if reusable else None
            payload={"character_id":char_a["id"],"user_message_id":user_message_id,"char_a":{"name":char_a["name"],"concept":char_a["concept"],"base_image":char_a["base_image_path"],"voice_sample":char_a["voice_sample_path"]},"char_b":{"name":char_b["name"],"concept":char_b["concept"],"base_image":char_b["base_image_path"],"voice_sample":char_b["voice_sample_path"]},"scene_text":normalized,"interaction_text":normalized,"dialogue_turns":confirmed,"caption":f"{char_a['name']} and {char_b['name']}","scene_id":reusable["id"] if reusable else None,"scene_context":reusable["description"] if reusable else "","scene_revision":reusable["revision"] if reusable else 0,"scene_aspect_mode":aspect,"scene_reference_image":asset["reference_image"] if asset and asset.get("ready") else None}
            job_id=enqueue_job(conn,0,"multi_character_video","heavy",payload,priority=10,context_id=cid)
            return jsonify({"job_id":job_id,"status":"queued"}),202
    @web.get("/api/v1/jobs/<int:job_id>")
    def job(job_id):
        with db_lock,get_db() as conn:
            cid=context_id(conn);row=conn.execute("SELECT id,job_type,status,result,progress_json FROM jobs WHERE id=? AND context_id=?",(job_id,cid)).fetchone()
            if not row:return _error("not_found","Job not found.",404)
            view=_job_view(row);view["result"]=json.loads(row["result"]) if row["result"] else None
            return jsonify(view)
    @web.post("/api/v1/jobs/<int:job_id>/retry")
    def retry_job(job_id):
        creation_states={"profile_generate":"profile_generating","character_image_batch":"image_queued",
                         "promote_reference":"promoting_reference","voice_sample_batch":"voice_generating",
                         "promote_voice":"promoting_voice"}
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            row=conn.execute("SELECT * FROM jobs WHERE id=? AND context_id=?",(job_id,cid)).fetchone()
            if not row:return _error("not_found","Job not found.",404)
            result=json.loads(row["result"] or "{}")
            if row["status"]!="failed" or result.get("cancelled") or row["job_type"] not in RETRYABLE_JOB_TYPES:
                return _error("state_conflict","This job cannot be retried.",409)
            payload=json.loads(row["payload"]);character_id=payload.get("character_id")
            if character_id and not conn.execute("SELECT 1 FROM characters WHERE id=?",(character_id,)).fetchone():
                return _error("state_conflict","The character used by this job no longer exists.",409)
            state=creation_states.get(row["job_type"])
            if state:
                conn.execute("DELETE FROM sessions WHERE context_id=? AND character_id=? AND state!='active'",(cid,character_id))
                conn.execute("INSERT INTO sessions(character_id,chat_id,context_id,state) VALUES(?,0,?,?)",(character_id,cid,state))
            payload["retry_of_job_id"]=job_id
            new_id=enqueue_job(conn,row["chat_id"],row["job_type"],row["weight"],payload,priority=row["priority"],context_id=cid)
            return jsonify({"job_id":new_id,"status":"queued","retry_of_job_id":job_id}),202

    @web.post("/api/v1/jobs/<int:job_id>/cancel")
    def cancel_job(job_id):
        paths=[];interrupt_comfy=False;comfy=None
        with db_lock,get_db() as conn:
            cid=context_id(conn);row=conn.execute("SELECT id,status,job_type,weight,payload FROM jobs WHERE id=? AND context_id=?",(job_id,cid)).fetchone()
            if not row:return _error("not_found","Job not found.",404)
            if row["status"] not in ("queued","dispatched","interrupted"):return _error("state_conflict","This job has already finished.",409)
            interrupt_comfy=row["status"]=="dispatched" and row["weight"]=="heavy"
            comfy=get_setting(conn,"endpoint_comfyui") or "http://127.0.0.1:8188"
            payload=json.loads(row["payload"]);character_id=payload.get("character_id")
            conn.execute("UPDATE jobs SET status='failed',result=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",(json.dumps({"text":"Cancelled by user","cancelled":True}),job_id))
            creation=conn.execute("SELECT id FROM sessions WHERE context_id=? AND character_id=? AND state!='active'",(cid,character_id)).fetchone() if character_id else None
            if creation:
                paths=[r[0] for r in conn.execute("SELECT relative_path FROM artifacts WHERE context_id=? AND character_id=?",(cid,character_id))]
                conn.execute("DELETE FROM message_artifacts WHERE artifact_id IN (SELECT id FROM artifacts WHERE context_id=? AND character_id=?)",(cid,character_id))
                conn.execute("DELETE FROM artifacts WHERE context_id=? AND character_id=?",(cid,character_id));conn.execute("DELETE FROM messages WHERE context_id=? AND character_id=?",(cid,character_id));conn.execute("DELETE FROM context_selections WHERE context_id=? AND character_id=?",(cid,character_id));conn.execute("DELETE FROM sessions WHERE id=?",(creation["id"],));conn.execute("DELETE FROM characters WHERE id=?",(character_id,))
            elif payload.get("user_message_id"):
                conn.execute("DELETE FROM messages WHERE id=? AND context_id=?",(payload["user_message_id"],cid))
        for relative in paths:
            try:resolve_artifact(relative).unlink(missing_ok=True)
            except (OSError,ValueError):pass
        if interrupt_comfy:
            try:requests.post(f"{comfy}/interrupt",timeout=5)
            except requests.RequestException:pass
        return "",204
    @web.get("/api/v1/artifacts/<int:artifact_id>")
    def artifact(artifact_id):
        with db_lock,get_db() as conn:
            cid=context_id(conn);row=conn.execute("""SELECT a.relative_path,a.mime_type FROM artifacts a
              WHERE a.id=? AND (a.context_id=? OR EXISTS(SELECT 1 FROM message_artifacts ma WHERE ma.artifact_id=a.id))""",(artifact_id,cid)).fetchone()
        if not row:return _error("not_found","Artifact not found.",404)
        try:path=resolve_artifact(row["relative_path"])
        except ValueError:return _error("not_found","Artifact not found.",404)
        if not path.is_file():return _error("not_found","Artifact file is missing.",404)
        downloading=request.args.get("download")=="1"
        return send_file(path,mimetype=row["mime_type"],conditional=True,
          as_attachment=downloading,
          download_name=f"castworkshop-{artifact_id}{path.suffix}" if downloading else None)
    @web.get("/api/v1/artifacts/<int:artifact_id>/video-chain")
    def video_chain(artifact_id):
        """Return the linear continuation ancestry ending at this video."""
        with db_lock,get_db() as conn:
            cid=context_id(conn);chain=[];seen=set();current=artifact_id
            while current is not None:
                if current in seen:return _error("state_conflict","The continuation chain contains a cycle.",409)
                seen.add(current)
                row=conn.execute("""SELECT a.id,a.kind,a.metadata_json FROM artifacts a
                  WHERE a.id=? AND (a.context_id=? OR EXISTS(
                    SELECT 1 FROM message_artifacts ma WHERE ma.artifact_id=a.id))""",(current,cid)).fetchone()
                if not row or row["kind"]!="video":return _error("not_found","A video in this continuation chain is unavailable.",404)
                chain.append(row["id"])
                try:metadata=json.loads(row["metadata_json"] or "{}")
                except (TypeError,ValueError):metadata={}
                parent=metadata.get("continuation_of_artifact_id")
                current=parent if isinstance(parent,int) else None
                if len(chain)>100:return _error("state_conflict","The continuation chain is too long.",409)
        chain.reverse()
        return jsonify({"artifact_id":artifact_id,"count":len(chain),"artifacts":[
          {"id":item,"url":f"/api/v1/artifacts/{item}"} for item in chain]})
    @web.get("/api/v1/diagnostics")
    def diagnostics():
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if get_setting(conn,"web_debug_mode")!="on":return _error("debug_disabled","Enable Debug mode in Settings first.",403)
            queue={row["status"]:row["count"] for row in conn.execute("SELECT status,COUNT(*) count FROM jobs GROUP BY status")}
            rows=conn.execute("SELECT id,job_type,status,result,progress_json,created_at,completed_at FROM jobs ORDER BY id DESC LIMIT 12").fetchall()
            jobs=[]
            for row in rows:
                view=_job_view(row)
                jobs.append({"id":view["id"],"type":view["type"],"status":view["status"],
                             "phase":view["progress"].get("phase"),"failure":view["failure"],
                             "created_at":row["created_at"],"completed_at":row["completed_at"]})
            ollama=get_setting(conn,"endpoint_ollama") or "http://127.0.0.1:11434"
            comfy=get_setting(conn,"endpoint_comfyui") or "http://127.0.0.1:8188"
            model=get_setting(conn,"current_model") or os.environ.get("OLLAMA_MODEL","qwen3-14b-64k")
            telegram=_telegram_state()
            content_mode=get_setting(conn,"content_mode") or defaults["content_mode"]
            aspect_mode=get_setting(conn,"aspect_mode") or defaults["aspect_mode"]
        def probe(base,path):
            started=time.monotonic()
            try:
                response=requests.get(base.rstrip("/")+path,timeout=3)
                return {"status":"online" if response.ok else "error","http_status":response.status_code,
                        "latency_ms":round((time.monotonic()-started)*1000)}
            except requests.RequestException:return {"status":"offline","http_status":None,"latency_ms":None}
        return jsonify({"generated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
          "runtime":{"platform":sys.platform,"python":".".join(map(str,sys.version_info[:3]))},
          "services":{"vps":{"status":"online"},"worker":_heartbeat("worker"),
                      "ollama":probe(ollama,"/api/tags"),"comfyui":probe(comfy,"/system_stats"),
                      "telegram":{"status":"active" if telegram["active"] else "disabled","configured":telegram["configured"],"restart_required":telegram["restart_required"]}},
          "queue":{"queued":queue.get("queued",0),"running":queue.get("dispatched",0),"failed":queue.get("failed",0),"completed":queue.get("done",0)},
          "configuration":{"content_mode":content_mode,"aspect_mode":aspect_mode,"model":model},
          "jobs":jobs})

    @web.patch("/api/v1/settings")
    def update_settings():
        body=request.get_json(silent=True) or {}
        allowed={"content_mode":("sfw","nsfw"),"aspect_mode":("vertical","cinematic")}
        with db_lock,get_db() as conn:
            cid=context_id(conn)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            for key,values in allowed.items():
                if key in body:
                    if body[key] not in values:return _error("validation_error",f"Invalid {key}.",422)
                    set_setting(conn,key,body[key])
            if "character_videos" in body:
                if not isinstance(body["character_videos"],bool):return _error("validation_error","character_videos must be boolean.",422)
                set_setting(conn,"character_videos","on" if body["character_videos"] else "off")
            if "debug_mode" in body:
                if not isinstance(body["debug_mode"],bool):return _error("validation_error","debug_mode must be boolean.",422)
                set_setting(conn,"web_debug_mode","on" if body["debug_mode"] else "off")
            if "model" in body:
                model=body["model"]
                if not isinstance(model,str) or not model.strip() or len(model)>200:return _error("validation_error","Model name is invalid.",422)
                set_setting(conn,"current_model",model.strip())
            for key in ("endpoint_ollama","endpoint_comfyui"):
                if key in body:
                    endpoint=body[key]
                    if not isinstance(endpoint,str) or len(endpoint)>500 or not re.fullmatch(r"https?://[^\s/]+(?::\d+)?(?:/[^\s]*)?",endpoint.strip()):
                        return _error("validation_error",f"{key} must be a valid HTTP or HTTPS URL.",422)
                    set_setting(conn,key,endpoint.strip().rstrip("/"))
            telegram=body.get("telegram")
            if telegram is not None:
                if not isinstance(telegram,dict) or not isinstance(telegram.get("enabled"),bool):
                    return _error("validation_error","Telegram settings are invalid.",422)
                token=telegram.get("token","")
                if not isinstance(token,str) or len(token)>256:return _error("validation_error","Telegram token is invalid.",422)
                try:_write_telegram_config(telegram["enabled"],token,telegram.get("remove_token") is True)
                except (ValueError,OSError) as exc:return _error("validation_error",str(exc),422)
            workflows=body.get("workflows",{})
            if not isinstance(workflows,dict):return _error("validation_error","workflows must be an object.",422)
            catalog={step["key"]:step for step in build_workflow_steps()}
            for key,value in workflows.items():
                if key not in catalog or value not in {x["key"] for x in catalog[key]["strategies"]}:return _error("validation_error",f"Invalid workflow for {key}.",422)
                set_setting(conn,f"workflow_{key}",value)
            return jsonify(data(conn,cid))

    @web.post("/api/v1/telegram/test")
    def test_telegram():
        body=request.get_json(silent=True) or {};token=body.get("token","")
        if not isinstance(token,str) or len(token)>256:return _error("validation_error","Telegram token is invalid.",422)
        token=token.strip() or _env_values().get("TELEGRAM_BOT_TOKEN","")
        if not token:return _error("validation_error","Enter or save a Telegram bot token first.",422)
        if not TELEGRAM_TOKEN_PATTERN.fullmatch(token):return _error("validation_error","Telegram token format is invalid.",422)
        try:
            response=requests.get(f"https://api.telegram.org/bot{token}/getMe",timeout=12)
            payload=response.json()
            if not response.ok or not payload.get("ok"):return _error("telegram_rejected","Telegram rejected this token.",422)
        except (requests.RequestException,ValueError):
            return _error("service_unavailable","Could not reach Telegram.",503)
        bot=payload.get("result") or {}
        return jsonify({"ok":True,"username":bot.get("username"),"display_name":bot.get("first_name")})

    @web.get("/api/v1/characters/<int:character_id>")
    def character_detail(character_id):
        with db_lock,get_db() as conn:
            cid=context_id(conn);row=conn.execute("SELECT id,name,concept,personality_notes,base_image_path IS NOT NULL image_ready,voice_sample_path IS NOT NULL voice_ready FROM characters WHERE id=?",(character_id,)).fetchone()
            if not row:return _error("not_found","Character not found.",404)
            result=dict(row);result["thumbnail_url"]=f"/api/v1/characters/{character_id}/thumbnail?size=full" if result.pop("image_ready") else None
            return jsonify(result)

    @web.get("/api/v1/characters/<int:character_id>/thumbnail")
    def character_thumbnail(character_id):
        with db_lock,get_db() as conn:
            context_id(conn);row=conn.execute("SELECT base_image_path FROM characters WHERE id=?",(character_id,)).fetchone()
            comfy=get_setting(conn,"endpoint_comfyui") or "http://127.0.0.1:8188"
        if not row or not row["base_image_path"]:return _error("not_found","Reference image not found.",404)
        try:
            response=requests.get(f"{comfy}/view",params={"filename":row["base_image_path"],"type":"input"},timeout=20)
            response.raise_for_status()
        except requests.RequestException:return _error("service_unavailable","Reference image is unavailable.",503)
        if request.args.get("size")=="full":
            return Response(response.content,mimetype=response.headers.get("Content-Type","image/png"),headers={"Cache-Control":"private, max-age=60"})
        try:
            source=Image.open(io.BytesIO(response.content)).convert("RGB")
            thumbnail=ImageOps.fit(source,(192,192),method=Image.Resampling.LANCZOS,centering=(0.5,0.18))
            output=io.BytesIO();thumbnail.save(output,"JPEG",quality=90,optimize=True)
            return Response(output.getvalue(),mimetype="image/jpeg",headers={"Cache-Control":"private, max-age=3600"})
        except (OSError,ValueError):
            return Response(response.content,mimetype=response.headers.get("Content-Type","image/png"),headers={"Cache-Control":"private, max-age=60"})

    @web.patch("/api/v1/characters/<int:character_id>")
    def update_character(character_id):
        body=request.get_json(silent=True) or {};changes={}
        for key in ("name","concept","personality_notes"):
            if key in body:
                value=body[key]
                if not isinstance(value,str) or not value.strip() or len(value)>6000:return _error("validation_error",f"{key} is invalid.",422)
                changes[key]=value.strip()
        if not changes:return _error("validation_error","No editable fields supplied.",422)
        with db_lock,get_db() as conn:
            cid=context_id(conn);row=conn.execute("SELECT * FROM characters WHERE id=?",(character_id,)).fetchone()
            if not row:return _error("not_found","Character not found.",404)
            if work_conflict(conn,cid):return _error("job_conflict","Finish the current generation or character setup first.",409)
            conn.execute("UPDATE characters SET "+",".join(f"{key}=?" for key in changes)+" WHERE id=?",(*changes.values(),character_id))
            job_id=None
            if "personality_notes" in changes:
                job_id=enqueue_job(conn,0,"edit_profile_generate","light",{"character_id":character_id,"personality_notes":changes["personality_notes"],"name":changes.get("name",row["name"])},context_id=cid)
            return jsonify({"status":"updated","job_id":job_id})

    @web.delete("/api/v1/characters/<int:character_id>")
    def delete_character(character_id):
        body=request.get_json(silent=True) or {}
        paths=[]
        with db_lock,get_db() as conn:
            cid=context_id(conn);row=conn.execute("SELECT name FROM characters WHERE id=?",(character_id,)).fetchone()
            if not row:return _error("not_found","Character not found.",404)
            if body.get("confirm_name")!=row["name"]:return _error("confirmation_required","Enter the exact character name.",409)
            if conn.execute("SELECT 1 FROM jobs WHERE status IN ('queued','dispatched') AND json_extract(payload,'$.character_id')=? LIMIT 1",(character_id,)).fetchone():return _error("job_conflict","Wait for this character's active job to finish.",409)
            artifacts=conn.execute("SELECT id,relative_path FROM artifacts WHERE character_id=?",(character_id,)).fetchall()
            artifact_ids=[r["id"] for r in artifacts];paths=[r["relative_path"] for r in artifacts]
            if artifact_ids:
                marks=",".join("?"*len(artifact_ids));conn.execute(f"DELETE FROM message_artifacts WHERE artifact_id IN ({marks})",artifact_ids);conn.execute(f"DELETE FROM artifacts WHERE id IN ({marks})",artifact_ids)
            conn.execute("DELETE FROM messages WHERE character_id=?",(character_id,))
            conn.execute("DELETE FROM context_selections WHERE character_id=?",(character_id,))
            conn.execute("DELETE FROM sessions WHERE character_id=?",(character_id,))
            conn.execute("DELETE FROM characters WHERE id=?",(character_id,))
            if get_setting(conn,"active_character_id")==str(character_id):set_shared_character(conn,None)
        for relative in paths:
            try:resolve_artifact(relative).unlink(missing_ok=True)
            except (OSError,ValueError):pass
        return "",204

    @web.get("/web")
    @web.get("/web/")
    def index():return send_from_directory(WEB_ROOT,"index.html")
    @web.get("/web/<path:asset>")
    def asset(asset):return send_from_directory(WEB_ROOT,asset if (WEB_ROOT/asset).is_file() else "index.html")
    return web
