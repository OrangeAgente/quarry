"""Every Jinja template must parse. Catches syntax errors (unclosed blocks,
bad filters) without needing to hit each route in a browser."""
import os

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = "templates"


@pytest.mark.parametrize(
    "name", sorted(n for n in os.listdir(TEMPLATE_DIR) if n.endswith(".html"))
)
def test_template_parses(name):
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    env.get_template(name)
