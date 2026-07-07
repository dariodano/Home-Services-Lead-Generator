#!/usr/bin/env python3
"""Web UI for the lead generator — a dead-simple local CRM.

Run:  python app.py   then open http://127.0.0.1:5050

- Start a search for any industry from the form on the home page.
- Each industry is its own list, stored in leads.db in this folder.
- Re-running a search only adds businesses you don't already have.
- Opening a lead's Maps link marks it done so you never re-click it.
"""

import threading

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

import db
import leadgen

HOST = "127.0.0.1"
PORT = 5050

app = Flask(__name__)

# One background search at a time; state is polled by the home page.
_lock = threading.Lock()
_state: dict = {"running": False, "query": None, "cell": 0, "total_cells": 0,
                "unique": 0, "new_leads": 0, "api_calls": 0,
                "error": None, "summary": None}


def _search_state() -> dict:
    with _lock:
        return dict(_state)


def _run_search_bg(query: str, radius: float, center: tuple[float, float],
                   include_social: bool) -> None:
    def progress(p: dict) -> None:
        with _lock:
            _state.update(p)

    try:
        summary = leadgen.run_search(query, radius_miles=radius, center=center,
                                     include_social=include_social,
                                     progress=progress)
        with _lock:
            _state["summary"] = summary
    except Exception as exc:
        with _lock:
            _state["error"] = str(exc)
    finally:
        with _lock:
            _state["running"] = False


@app.get("/")
def index():
    return render_template("index.html",
                           industries=db.list_industries(),
                           state=_search_state(),
                           has_api_key=leadgen.get_api_key() is not None,
                           default_center="%g,%g" % leadgen.DEFAULT_CENTER,
                           default_radius=int(leadgen.DEFAULT_RADIUS_MILES))


@app.post("/search")
def start_search():
    query = (request.form.get("query") or "").strip()
    if not query:
        return redirect(url_for("index"))

    with _lock:
        if _state["running"]:
            return redirect(url_for("index"))
        _state.update({"running": True, "query": query, "cell": 0,
                       "total_cells": 0, "unique": 0, "new_leads": 0,
                       "api_calls": 0, "error": None, "summary": None})

    try:
        radius = float(request.form.get("radius") or leadgen.DEFAULT_RADIUS_MILES)
        center = leadgen.parse_center(
            request.form.get("center") or "%g,%g" % leadgen.DEFAULT_CENTER)
    except (ValueError, leadgen.LeadGenError) as exc:
        with _lock:
            _state.update({"running": False, "error": str(exc)})
        return redirect(url_for("index"))

    include_social = request.form.get("include_social") == "on"
    threading.Thread(target=_run_search_bg,
                     args=(query, radius, center, include_social),
                     daemon=True).start()
    return redirect(url_for("index"))


@app.get("/api/search-status")
def search_status():
    return jsonify(_search_state())


@app.get("/industry/<int:industry_id>")
def industry(industry_id: int):
    ind = db.get_industry(industry_id)
    if ind is None:
        abort(404)
    leads = db.get_leads(industry_id)
    hide_done = request.args.get("show") != "all"
    return render_template("industry.html", industry=ind, leads=leads,
                           hide_done=hide_done,
                           remaining=sum(1 for l in leads if not l["clicked"]))


@app.post("/lead/<int:lead_id>/clicked")
def lead_clicked(lead_id: int):
    data = request.get_json(silent=True) or {}
    if not db.set_clicked(lead_id, bool(data.get("clicked", True))):
        abort(404)
    return jsonify({"ok": True})


@app.post("/industry/<int:industry_id>/delete")
def industry_delete(industry_id: int):
    db.delete_industry(industry_id)
    return redirect(url_for("index"))


if __name__ == "__main__":
    print(f"\n  Lead CRM running — open http://{HOST}:{PORT} in your browser\n")
    app.run(host=HOST, port=PORT, debug=False)
