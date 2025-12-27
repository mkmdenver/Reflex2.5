from datetime import datetime, timezone
from models import CapabilityDoc, SessionState

def capability_for(account_id: str, session: SessionState) -> CapabilityDoc:
    broker = account_id.split(":")[0]
    native = {
        "market": session == "REGULAR",
        "limit": True,
        "stop": session == "REGULAR",
        "stop_limit": session == "REGULAR",
        "trailing": False,
        "oco": session == "REGULAR",
        "extended_hours_flag": session != "REGULAR",
    }
    return CapabilityDoc(
        broker=broker,
        account_id=account_id,
        session=session,
        native=native,
        constraints={"tif": ["day","gtc","ioc"], "min_qty": 1, "route_select": False},
        as_of=datetime.now(timezone.utc).isoformat(),
        source="profile",
    )
