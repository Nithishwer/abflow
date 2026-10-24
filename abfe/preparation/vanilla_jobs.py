from importlib.abc import Traversable


def copy_jobscripts_vanilla(template_dir: Traversable, name: str, slots: int):
    for template_file in template_dir.iterdir():
        if template_file.name.endswith(".sh"):
            with template_file.open("r") as f:
                content = f.read()

            content = content.replace("$NAME$", name).replace(
                "$NO_OF_NODES$", str(slots)
            )

            with open(template_file.name, "w") as f_out:
                f_out.write(content)
