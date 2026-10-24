DATASET_COLORS = {
    "deflorian-set1": "#0fffdb",
    "piersanti": "#ffd166",
    "yuan": "#9500ff",
    "heffernan": "#ff0059",
    "thoma": "#ff8c00",
    "sigma2": "#b4e600",
}


STAGES = {
    "not_started": ("Not started", "#ffd166"),
    "inputs": ("Inputs ready", "#0fffdb"),
    "prepared": ("Prepared", "#9500ff"),
    "queued": ("Queued", "#ffd166"),
    "equilibration": ("Equilibrating", "#ff8c00"),
    "production": ("Production observed", "#ff0059"),
    "analysis": ("Analysis on file", "#0fffdb"),
    "converged": ("Converged", "#b4e600"),
    "complete": ("Complete", "#0fffdb"),
    "unknown": ("Unknown", "#8a9199"),
}


REMARK_COLORS = {
    "Attention required": "#ff0059",
    "Waiting": "#ff8c00",
    "OK": "#0fffdb",
    "Unknown": "#8a9199",
}


TILE_STATUSES = {
    "attention": ("Attention required", REMARK_COLORS["Attention required"]),
    "not_started": ("Not started", STAGES["unknown"][1]),
    "complete": ("Complete (Vanilla) / Converged (FEP)", "#0ad2ff"),
    "simulation": ("Under simulation", STAGES["converged"][1]),
}
