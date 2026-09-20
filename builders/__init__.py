from .hugo import build as build_hugo
from .jekyll import build as build_jekyll
from .raw import build as build_raw

BUILDERS = {
    "raw": build_raw,
    "hugo": build_hugo,
    "jekyll": build_jekyll,
}
