def number(value):
    return "?" if value is None else f"{value:.3f}".rstrip("0").rstrip(".")
