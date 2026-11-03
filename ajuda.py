"""Tutorial rápido do painel (/ajuda) e o convite para vê-lo no primeiro acesso."""
from flask import Blueprint, g, redirect, render_template, request, url_for

bp = Blueprint("ajuda", __name__)


@bp.route("/ajuda")
def tutorial():
    import meu
    if not meu.pref("tutorial_visto"):
        meu.salvar_pref("tutorial_visto", True)
    return render_template("ajuda.html", aba=None)


@bp.post("/ajuda/dispensar")
def dispensar():
    import meu
    meu.salvar_pref("tutorial_visto", True)
    return redirect(request.referrer or url_for("index"))


@bp.app_context_processor
def _ctx():
    u = g.get("usuario")
    if not u:
        return {}
    import meu
    return {"convite_tutorial": not meu.pref("tutorial_visto") and request.endpoint not in ("ajuda.tutorial",)}
