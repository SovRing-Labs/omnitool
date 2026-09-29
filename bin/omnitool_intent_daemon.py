#!/usr/bin/env python3
"""
Omnitool BERT Daemon — Hotloaded Intelligence Layer with Semantic Cache & Error Fix-Map

Features:
- BERT intent classifier (all-MiniLM-L6-v2) — hotloaded, ~90MB RAM
- Semantic Result Cache (FAISS HNSW) — 0.98+ similarity → instant cached returns
- Error Vector Fix-Map — embed(error + failed_payload) → known-good rewrite
- 10-minute TTL auto-shutdown after last request
- REST API for fleet-exec and MCP server integration
"""

import json
import os
import sys
import time
import threading
import hashlib
import sqlite3
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
from contextlib import contextmanager

import numpy as np
import faiss
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

# Add ai-stack bin to path for imports
AI_STACK_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AI_STACK_ROOT / "bin"))

from omnitool_intent_classifier import get_classifier, OmnitoolIntentClassifier

# Configuration
DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = 8498
TTL_SECONDS = int(os.environ.get("OMNITOOL_DAEMON_TTL_SECONDS", 600))  # under systemd Restart=always, set high/disabled — self-shutdown fights the supervisor
CACHE_DIR = AI_STACK_ROOT / "db" / "omnitool_daemon"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Semantic cache settings
SIMILARITY_THRESHOLD = 0.98
MAX_CACHE_ENTRIES = 10000
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 output dimension

# Fix-map settings
FIXMAP_DB = CACHE_DIR / "fixmap.db"

# Global State
_classifier: Optional[OmnitoolIntentClassifier] = None
_last_use = time.time()
_last_use_lock = threading.Lock()

# FAISS index for semantic cache
_faiss_index: Optional[faiss.IndexHNSWFlat] = None
_cache_metadata: List[Dict[str, Any]] = []
_cache_lock = threading.Lock()

# Database Setup
@contextmanager
def fixmap_db():
    conn = sqlite3.connect(FIXMAP_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_fixmap_db():
    with fixmap_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fix_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                error_hash TEXT NOT NULL,
                error_text TEXT NOT NULL,
                failed_payload_hash TEXT NOT NULL,
                failed_payload TEXT NOT NULL,
                fixed_payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                use_count INTEGER DEFAULT 1,
                UNIQUE(error_hash, failed_payload_hash)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_error_hash ON fix_map(error_hash)
        """)
        conn.commit()

# FAISS Index Management
def init_faiss_index():
    global _faiss_index, _cache_metadata
    
    index_path = CACHE_DIR / "semantic_cache.faiss"
    meta_path = CACHE_DIR / "semantic_cache_meta.json"
    
    if index_path.exists() and meta_path.exists():
        _faiss_index = faiss.read_index(str(index_path))
        with open(meta_path, 'r') as f:
            _cache_metadata = json.load(f)
        print(f"[daemon] Loaded FAISS index: {_faiss_index.ntotal} vectors")
    else:
        _faiss_index = faiss.IndexHNSWFlat(EMBEDDING_DIM, 32)
        _faiss_index.hnsw.efConstruction = 200
        _faiss_index.hnsw.efSearch = 128
        _cache_metadata = []
        print("[daemon] Created new FAISS HNSW index")

def save_faiss_index():
    index_path = CACHE_DIR / "semantic_cache.faiss"
    meta_path = CACHE_DIR / "semantic_cache_meta.json"
    faiss.write_index(_faiss_index, str(index_path))
    with open(meta_path, 'w') as f:
        json.dump(_cache_metadata, f)

def get_embedding(text: str) -> np.ndarray:
    model = _classifier._get_model() if _classifier else get_classifier()._get_model()
    emb = model.encode([text], convert_to_numpy=True, normalize_embeddings=True)[0]
    return emb.astype(np.float32)

# Semantic Cache Operations
def cache_key(payload: Dict[str, Any]) -> str:
    ops = payload.get("operations", [])
    normalized_ops = []
    for op in ops:
        norm_op = {k: v for k, v in op.items() if k not in ("timeout_s", "timeout_ms", "detach", "log_path")}
        normalized_ops.append(norm_op)
    normalized_ops.sort(key=lambda x: json.dumps(x, sort_keys=True))
    payload_str = json.dumps({"operations": normalized_ops}, sort_keys=True)
    return hashlib.sha256(payload_str.encode()).hexdigest()[:16]

def semantic_cache_lookup(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    global _faiss_index, _cache_metadata
    
    if _faiss_index is None or _faiss_index.ntotal == 0:
        return None
    
    text_parts = []
    for op in payload.get("operations", []):
        op_type = op.get("type", "unknown")
        if op_type == "bash" and "command" in op:
            text_parts.append("bash " + op["command"])
        elif op_type == "read" and "path" in op:
            text_parts.append("read " + op["path"])
        elif op_type == "write" and "path" in op:
            text_parts.append("write " + op["path"])
        elif op_type == "patch" and "diff" in op:
            text_parts.append("patch")
        elif op_type == "sql" and "database" in op:
            text_parts.append("sql " + op["database"])
        else:
            text_parts.append(op_type)
    
    query_text = " ; ".join(text_parts)
    query_emb = get_embedding(query_text).reshape(1, -1)
    
    with _cache_lock:
        distances, indices = _faiss_index.search(query_emb, 1)
    
    if len(indices[0]) > 0 and indices[0][0] != -1:
        similarity = 1.0 - distances[0][0]
        if similarity >= SIMILARITY_THRESHOLD:
            idx = indices[0][0]
            meta = _cache_metadata[idx]
            if _is_cache_valid(meta):
                print(f"[cache] HIT similarity={similarity:.4f} key={meta['key']}")
                return meta["result"]
            else:
                print(f"[cache] STALE key={meta['key']}")
    
    return None

def _is_cache_valid(meta: Dict[str, Any]) -> bool:
    for file_check in meta.get("file_checks", []):
        path = file_check["path"]
        expected_mtime = file_check["mtime_ns"]
        try:
            actual_mtime = Path(path).expanduser().resolve().stat().st_mtime_ns
            if actual_mtime != expected_mtime:
                return False
        except OSError:
            return False
    return True

def semantic_cache_store(payload: Dict[str, Any], result: Dict[str, Any]):
    global _faiss_index, _cache_metadata
    
    text_parts = []
    file_checks = []
    
    for op in payload.get("operations", []):
        op_type = op.get("type", "unknown")
        if op_type == "bash" and "command" in op:
            text_parts.append("bash " + op["command"])
        elif op_type == "read" and "path" in op:
            text_parts.append("read " + op["path"])
            path = Path(op["path"]).expanduser().resolve()
            if path.exists():
                file_checks.append({"path": str(path), "mtime_ns": path.stat().st_mtime_ns})
        elif op_type == "write" and "path" in op:
            text_parts.append("write " + op["path"])
        elif op_type == "patch" and "diff" in op:
            text_parts.append("patch")
        elif op_type == "sql" and "database" in op:
            text_parts.append("sql " + op["database"])
        else:
            text_parts.append(op_type)
    
    query_text = " ; ".join(text_parts)
    query_emb = get_embedding(query_text).reshape(1, -1)
    key = cache_key(payload)
    
    with _cache_lock:
        if _faiss_index.ntotal > 0:
            distances, indices = _faiss_index.search(query_emb, 1)
            if len(indices[0]) > 0 and indices[0][0] != -1:
                similarity = 1.0 - distances[0][0]
                if similarity >= SIMILARITY_THRESHOLD:
                    idx = indices[0][0]
                    _cache_metadata[idx] = {
                        "key": key,
                        "text": query_text,
                        "result": result,
                        "file_checks": file_checks,
                        "timestamp": time.time()
                    }
                    return
        
        _faiss_index.add(query_emb)
        _cache_metadata.append({
            "key": key,
            "text": query_text,
            "result": result,
            "file_checks": file_checks,
            "timestamp": time.time()
        })
        
        if len(_cache_metadata) > MAX_CACHE_ENTRIES:
            _cache_metadata.pop(0)
            rebuild_faiss_index()
        else:
            save_faiss_index()

def rebuild_faiss_index():
    global _faiss_index, _cache_metadata
    _faiss_index = faiss.IndexHNSWFlat(EMBEDDING_DIM, 32)
    _faiss_index.hnsw.efConstruction = 200
    _faiss_index.hnsw.efSearch = 128
    
    if _cache_metadata:
        model = _classifier._get_model() if _classifier else get_classifier()._get_model()
        texts = [m["text"] for m in _cache_metadata]
        embeddings = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True).astype(np.float32)
        _faiss_index.add(embeddings)
    
    save_faiss_index()

# Error Fix-Map Operations
def error_hash(error_text: str) -> str:
    return hashlib.sha256(error_text.encode()).hexdigest()[:16]

def payload_hash(payload: Dict[str, Any]) -> str:
    payload_str = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(payload_str.encode()).hexdigest()[:16]

def fixmap_lookup(error_text: str, failed_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    e_hash = error_hash(error_text)
    p_hash = payload_hash(failed_payload)
    
    with fixmap_db() as conn:
        row = conn.execute(
            "SELECT fixed_payload FROM fix_map WHERE error_hash = ? AND failed_payload_hash = ?",
            (e_hash, p_hash)
        ).fetchone()
        
        if row:
            conn.execute(
                "UPDATE fix_map SET use_count = use_count + 1 WHERE error_hash = ? AND failed_payload_hash = ?",
                (e_hash, p_hash)
            )
            conn.commit()
            return json.loads(row["fixed_payload"])
    
    return None

def fixmap_store(error_text: str, failed_payload: Dict[str, Any], fixed_payload: Dict[str, Any]):
    e_hash = error_hash(error_text)
    p_hash = payload_hash(failed_payload)
    
    with fixmap_db() as conn:
        try:
            conn.execute(
                """INSERT INTO fix_map (error_hash, error_text, failed_payload_hash, 
                   failed_payload, fixed_payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (e_hash, error_text, p_hash, 
                 json.dumps(failed_payload), json.dumps(fixed_payload), time.time())
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE fix_map SET use_count = use_count + 1 WHERE error_hash = ? AND failed_payload_hash = ?",
                (e_hash, p_hash)
            )
            conn.commit()

# Request/Response Models
class ClassifyRequest(BaseModel):
    payload: Dict[str, Any]

class ClassifyResponse(BaseModel):
    intent: str
    intent_confidence: float
    safety: str
    safety_confidence: float
    is_anomaly: bool
    per_operation: List[Dict[str, Any]]
    cached_result: Optional[Dict[str, Any]] = None
    fixed_payload: Optional[Dict[str, Any]] = None

class ExecuteRequest(BaseModel):
    payload: Dict[str, Any]
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

class HealthResponse(BaseModel):
    status: str
    model: str
    cache_size: int
    fixmap_size: int
    uptime_seconds: float

# FastAPI App
app = FastAPI(title="Omnitool BERT Daemon")

@app.on_event("startup")
async def startup():
    global _classifier, _last_use
    print("[daemon] Starting up...", flush=True)
    init_fixmap_db()
    print("[daemon] Fixmap DB init done", flush=True)
    init_faiss_index()
    print("[daemon] FAISS index init done", flush=True)
    _classifier = get_classifier()
    print("[daemon] Classifier loaded", flush=True)
    _last_use = time.time()
    print("[daemon] Ready", flush=True)

@app.post("/classify")
async def classify(request: ClassifyRequest):
    global _last_use, _classifier
    with _last_use_lock:
        _last_use = time.time()
    
    payload = request.payload
    
    try:
        if _classifier is None:
            _classifier = get_classifier()
        
        has_write = any(op.get("type") in ("write", "patch", "sql") for op in payload.get("operations", []))
        if not has_write:
            cached = semantic_cache_lookup(payload)
            if cached:
                print(f"[daemon] Cache HIT for payload", flush=True)
                return {"intent": "cached", "intent_confidence": 1.0, "safety": "safe", "safety_confidence": 1.0, "is_anomaly": False, "per_operation": [], "cached_result": cached}
        
        print(f"[daemon] Classifying payload with {len(payload.get('operations', []))} ops", flush=True)
        intent_result = _classifier.classify_payload(payload)
        print(f"[daemon] Classification done: intent={intent_result['intent']}", flush=True)
        
        return {
            "intent": intent_result["intent"],
            "intent_confidence": intent_result["intent_confidence"],
            "safety": intent_result["safety"],
            "safety_confidence": intent_result["safety_confidence"],
            "is_anomaly": intent_result["is_anomaly"],
            "per_operation": intent_result["per_operation"]
        }
    except Exception as e:
        print(f"[daemon] ERROR in classify: {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/execute")
async def execute(request: ExecuteRequest):
    global _last_use
    with _last_use_lock:
        _last_use = time.time()
    
    payload = request.payload
    
    if request.error:
        fixed = fixmap_lookup(request.error, payload)
        if fixed:
            return {"fixed_payload": fixed, "source": "fixmap"}
        return {"fixed_payload": None}
    
    elif request.result and request.result.get("ok"):
        has_write = any(op.get("type") in ("write", "patch", "sql") for op in payload.get("operations", []))
        if not has_write:
            semantic_cache_store(payload, request.result)
    
    return {"status": "recorded"}

@app.post("/fixmap/learn")
async def fixmap_learn(error_text: str, failed_payload: Dict[str, Any], fixed_payload: Dict[str, Any]):
    fixmap_store(error_text, failed_payload, fixed_payload)
    return {"status": "learned"}

@app.get("/health", response_model=HealthResponse)
async def health():
    global _last_use
    with fixmap_db() as conn:
        fixmap_count = conn.execute("SELECT COUNT(*) FROM fix_map").fetchone()[0]
    
    return HealthResponse(
        status="ok",
        model="all-MiniLM-L6-v2",
        cache_size=len(_cache_metadata),
        fixmap_size=fixmap_count,
        uptime_seconds=time.time() - _last_use
    )

# TTL Monitor
def ttl_monitor():
    global _last_use
    while True:
        time.sleep(60)
        with _last_use_lock:
            if time.time() - _last_use > TTL_SECONDS:
                print(f"[daemon] TTL expired ({TTL_SECONDS}s), saving state and shutting down")
                save_faiss_index()
                os._exit(0)

# Main Entry
if __name__ == "__main__":
    threading.Thread(target=ttl_monitor, daemon=True).start()
    uvicorn.run(
        app,
        host=DAEMON_HOST,
        port=DAEMON_PORT,
        log_level="warning",
        access_log=False
    )