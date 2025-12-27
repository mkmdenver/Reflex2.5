from __future__ import annotations
from flask import Blueprint, jsonify
bp=Blueprint("home", __name__)

@bp.get("/")
def home():
    return jsonify({"message":"Hello from Portfolio!", "status":"ok"})