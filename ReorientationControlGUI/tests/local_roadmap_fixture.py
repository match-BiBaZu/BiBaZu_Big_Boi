"""Historical integration data lives in the external roadmap catalogue."""
from bibazu_reorientation.resources import roadmap_directory

def fixture_directory():
    return roadmap_directory() / "legacy_catalogue/originals/Df1a_roadmap_verified"
