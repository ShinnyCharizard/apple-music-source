"""The same staged settings envelope used by the other account plugins."""


def setup_document(document, step, identity, waiting, locale="en"):
    connected = identity == "done"
    document["wizard"] = {"step": step, "total": 3, "id": identity, "waiting": waiting}
    document.setdefault("progress", {"done": 3 if connected else step - 1, "total": 3})
    labels = {
        "components": ("Prepare playback components", "Prepara los componentes de reproducción"),
        "account": ("Connect your Apple Account in the browser", "Conecta tu cuenta Apple en el navegador"),
        "verification": ("Complete account verification in the browser", "Completa la verificación en el navegador"),
    }
    if connected:
        label = "Listo: Apple Music está conectado" if locale.startswith("es") else "Ready: Apple Music is connected"
    else:
        en, es = labels[identity]
        label = f"Paso {step} de 3: {es}" if locale.startswith("es") else f"Step {step} of 3: {en}"
    document["wizard"]["label"] = label
    if document["status"].get("phase") != "failed":
        document["status"]["label"] = label
    return document
