from app.schemas import ListingExtraction
from app.rules import apply_rules

CFG={"housing_rules":{"max_warm_rent_single_eur":550,"min_duration_months":6,
"whole_flat_exception":{"enabled":True,"max_effective_warm_rent_per_person_eur":550},
"warnings":{"furniture_takeover_over_eur":400,"deposit_over_cold_rent_multiple":2}}}

def test_wbs(): assert apply_rules(ListingExtraction(wbs_required=True,confidence=.9),CFG).decision=="SKIP"
def test_short(): assert apply_rules(ListingExtraction(minimum_duration_months=5,confidence=.9),CFG).decision=="SKIP"
def test_rent(): assert apply_rules(ListingExtraction(housing_type="wg_room",warm_rent_eur=600,confidence=.9),CFG).decision=="SKIP"
def test_flat():
    r=apply_rules(ListingExtraction(housing_type="whole_flat",warm_rent_eur=1100,realistic_residents=3,confidence=.9),CFG)
    assert r.decision=="APPLY"
def test_warning():
    r=apply_rules(ListingExtraction(furniture_takeover_eur=500,confidence=.9),CFG)
    assert r.decision=="APPLY" and r.warnings
