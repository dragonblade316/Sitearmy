from .hugo import build as build_hugo
from .jekyll import build as build_jekyll
from .quartz import build as build_quartz
from .raw import build as build_raw

BUILDERS = {
    "raw": build_raw,
    "hugo": build_hugo,
    "jekyll": build_jekyll,
    "quartz": build_quartz,
}
