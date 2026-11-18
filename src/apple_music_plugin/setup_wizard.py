"""Native four-step account assistant with independent web and audio state."""
from .i18n import settings_text, translate

STEPS = [("components", "Prepare", "Prepare playback"), ("web", "Library", "Connect your web library"),
         ("audio", "Lossless", "Connect lossless playback"), ("ready", "Ready", "Everything is connected")]


def action(key, label, fields=()):
    value = {"key": key, "label": label, "fields": list(fields)}
    if key in {"web_disconnect", "audio_disconnect", "sign_out"}:
        value["destructive"] = "account"
    return value


def field(key, label, secret=False, limit=320):
    return {"key": key, "label": label, "description": "", "required": True,
            "control": {"type": "secret" if secret else "text", "max_length": limit}}


def document(service, notice=None):
    components = service.components_ready()
    native = service.login_status()
    operation = service.audio_setup.snapshot()
    web = service.web_session.connected
    completed = [components, web, bool(native.get("signed_in")), False]
    first = next((index + 1 for index, done in enumerate(completed) if not done), 4)
    if service.setup_step == 4 and first < 4 and not service.setup_review:
        service.setup_step = first
    if service.setup_step is None or (not service.setup_review and service.setup_step < 4 and completed[service.setup_step - 1]):
        service.setup_step = first
    step = service.setup_step
    fields, actions, links, content = [], [], [], []
    waiting = False
    phase = "pending"
    description = "Two connections, one experience. Your web library and lossless engine keep separate sessions."
    if completed[step - 1] and service.setup_review:
        phase = "ready"
        description = "This step is complete. Continue without connecting again."
        actions = [action("wizard_next", "Continue")]
        progress = None
    elif step == 1:
        base = service.component_settings()
        actions = base["form"]["actions"]
        phase = base["status"]["phase"]
        if phase == "failed":
            notice = base["status"].get("description")
        description = "The local engine is included. Download its playback components once; they will be verified before use."
        waiting = phase == "running"
        content = [{"text": "Approximate download: 65 MiB. Your password is never sent to the component server."}]
        progress = base.get("progress")
    elif step == 2:
        description = "Authorize Apple to show your library, recommendations and playlists. Use the same account you will use for listening."
        if service.web_login.active:
            waiting, phase = True, "running"
            actions = [action("web_cancel", "Cancel authorization")]
            links = [{"label": "Open Apple authorization", "url": service.web_login.url, "primary": True}]
            content = [{"text": "After you finish in the browser, this step will advance automatically. You can reopen the window if you closed it."}]
        else:
            actions = [action("web_connect", "Connect web library")]
        if service.web_login.error:
            notice = service.web_login.error
            phase = "running" if service.web_login.active else "failed"
            if service.web_login.active:
                actions = [action("web_connect", "Retry authorization"), action("web_cancel", "Cancel")]
    elif step == 3:
        description = "This connection lets the local engine play lossless audio. Use the same Apple Account as your library."
        current = native.get("phase")
        if current == "challenge":
            fields = [field("reply", "Six-digit verification code", limit=6)]
            actions = [action("challenge", "Verify code", ["reply"]), action("audio_cancel", "Cancel connection")]
        elif current in {"pending", "recovering"}:
            phase, waiting = "running", True
            description = native.get("message") or "Apple is checking the session. A code field will appear if your account requests one."
            actions = [] if operation["action"] == "cancel" and operation["busy"] else [action("audio_cancel", "Cancel connection")]
            if operation["error"]:
                notice = operation["error"]
        elif current == "runtime_failed":
            phase = "failed"
            description = native.get("message", "The playback engine could not start.")
            actions = [action("retry_runtime", "Retry engine")]
        else:
            fields = [field("email", "Apple Account"), field("password", "Password", True, 512)]
            actions = [action("sign_in", "Connect lossless playback now", ["email", "password"])]
            content = [{"text": "Your password and code are used only to connect. They are not saved or shown again."}]
            if current in {"failed", "expired"}:
                phase = "failed"
                notice = native.get("message") or service.auth_failure or "The connection did not complete. Check your details and try again."
    else:
        phase = "ready" if all(completed[:3]) else "pending"
        description = "Your library and lossless playback are connected. You can now browse and listen."
        content = [{"text": "Web library: connected · Lossless playback: connected"},
                   {"text": "Track quality is confirmed during playback. Connecting accounts does not perform an audio test."}]
        actions = [action("verify_connections", "Check connections"), action("web_disconnect", "Disconnect library"),
                   action("audio_disconnect", "Disconnect playback")]
    if step > 1 and not waiting:
        actions.append(action("wizard_back", "Back"))
    steps = [{"id": key, "label": label, "state": "complete" if completed[i] else "current" if i + 1 == step else "upcoming"}
             for i, (key, label, _) in enumerate(STEPS)]
    result = {"revision": service.revision, "wizard": {"step": step, "total": 4, "waiting": waiting, "steps": steps,
              "title": STEPS[step - 1][2], "eyebrow": "APPLE MUSIC · SETUP", "description": description},
              "form": {"fields": fields, "actions": actions, "unsupported": []}, "values": {}, "links": links,
              "status": {"phase": phase, "label": translate(service.locale, "Step {step} of 4", step=step), "description": ""},
              "content": content, "notices": [notice] if notice else []}
    if step == 1 and progress:
        result["progress"] = progress
    return settings_text(result, service.locale)
