from typing import Any
import httpx
from mcp.server.fastmcp import FastMCP

# initialize FastMCP server
mcp = FastMCP("weather")

# constants
NWS_API_BASE = "https://api.weather.gov"
USER_AGENT = "weather-app/1.0"

async def make_nws_request(url: str) -> dict[str, Any] | None:
    """Make a request to the NWS API with proper error handling."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/geo+json"
    }
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, timeout=30.0)
            response.raise_for_status()
            return response.json()
        except Exception:
            return None

def format_alert(feature: dict) -> str:
    """Format an alert feature into a readable string."""
    props = feature["properties"]
    return f"""
Event: {props.get('event', 'Unknown')}
Area: {props.get('areaDesc', 'Unknown')}
Severity: {props.get('severity', 'Unknown')}
Description: {props.get('description', 'No description available')}
Instructions: {props.get('instruction', 'No specific instructions provided')}
"""

@mcp.tool()
async def check_medicaid_eligibility(
    age: int | None = None,
    annual_income: float | None = None,
    household_size: int | None = None,
    blind_or_disabled: bool | None = None,
    pregnant: bool | None = None,
    nursing_home: bool | None = None,
    under_21: bool | None = None,
    refugee: bool | None = None,
    cancer_screening_recipient: bool | None = None,
) -> str:
    """Check a user's Medicaid medical eligibility using the provided criteria.

    Eligibility criteria (any one of the following qualifies):
    - Over the age of 65
    - Blind or disabled
    - Pregnant
    - In a nursing or intermediate care home or a skilled nursing facility
    - Under the age of 21
    - A refugee living in the U.S. temporarily
    - A recipient of either cervical or breast cancer screening
    OR
    - Meet income limits for free medical based on household size (see table below)

    Income limits (annual):
    One person: $20,783
    Two people: $28,208
    Three people: $35,632
    Four people: $43,056
    Five people: $50,481
    Over five people: add $7,425 for each additional household member

    This function will return a short "MISSING" prompt if required parameters are None so callers
    can supply the missing values instead of blocking on input().
    """

    # required parameters to evaluate income-based eligibility: annual_income and household_size.
    # other boolean flags are optional but when None we will treat them as False for evaluation after
    # prompting the caller to supply missing parameters if both income and household_size are absent.
    missing = []
    if age is None:
        missing.append("age")
    if annual_income is None and household_size is None:
        # if neither income nor household size provided, we can't evaluate income path.
        missing.extend([p for p in ("annual_income", "household_size")])

    if missing:
        # deduplicate while preserving order
        seen = set()
        dedup = []
        for x in missing:
            if x not in seen:
                seen.add(x)
                dedup.append(x)
        return f"MISSING: {', '.join(dedup)} -> Please provide these parameters to evaluate eligibility."

    # validate numeric inputs
    try:
        age = int(age)
    except Exception:
        return "Invalid input: age must be an integer"
    if age < 0 or age > 130:
        return "Invalid input: age must be between 0 and 130"

    try:
        if annual_income is not None:
            annual_income = float(annual_income)
    except Exception:
        return "Invalid input: annual_income must be a number"
    if annual_income is not None and annual_income < 0:
        return "Invalid input: annual_income must be non-negative"

    if household_size is not None:
        try:
            household_size = int(household_size)
        except Exception:
            return "Invalid input: household_size must be an integer"
        if household_size <= 0:
            return "Invalid input: household_size must be a positive integer"

    # normalize boolean-like flags: treat None as False for the optional criteria checks
    def _coerce_bool(val: Any) -> bool | str:
        if isinstance(val, bool):
            return val
        if val is None:
            return False
        if isinstance(val, str):
            v = val.strip().lower()
            if v in ("y", "yes", "true", "1"):
                return True
            if v in ("n", "no", "false", "0"):
                return False
        return "INVALID"

    for name in ("blind_or_disabled", "pregnant", "nursing_home", "under_21", "refugee", "cancer_screening_recipient"):
        val = locals()[name]
        coerced = _coerce_bool(val)
        if coerced == "INVALID":
            return f"Invalid input: {name} must be boolean-like (true/false, yes/no) or omitted"
        locals()[name] = coerced  # type: ignore[index]

    # evaluate the categorical eligibility rules (any one qualifies)
    categorical_reasons = []
    if age > 65:
        categorical_reasons.append("Over 65")
    if blind_or_disabled:
        categorical_reasons.append("Blind or disabled")
    if pregnant:
        categorical_reasons.append("Pregnant")
    if nursing_home:
        categorical_reasons.append("Nursing or skilled nursing facility resident")
    if under_21 or age < 21:
        categorical_reasons.append("Under 21")
    if refugee:
        categorical_reasons.append("Temporary refugee in U.S.")
    if cancer_screening_recipient:
        categorical_reasons.append("Recipient of cervical or breast cancer screening")

    if categorical_reasons:
        reason = "; ".join(categorical_reasons)
        return f"ELIGIBLE: categorical match -> {reason} (age={age})"

    # if no categorical match, evaluate income-based eligibility. Both annual_income and household_size
    # are required for this path.
    if annual_income is None or household_size is None:
        return "MISSING: annual_income and household_size required to evaluate income-based eligibility"

    # income limits table (from Medi-Cal guidelines)
    limits_by_size = {
        1: 20783.0,
        2: 28208.0,
        3: 35632.0,
        4: 43056.0,
        5: 50481.0,
    }
    if household_size <= 5:
        limit = limits_by_size[household_size]
    else:
        extra = household_size - 5
        limit = limits_by_size[5] + extra * 7425.0

    if annual_income <= limit:
        return f"ELIGIBLE: income-based -> household_size={household_size}, income=${annual_income:.2f} <= limit=${limit:.2f}"
    else:
        return f"NOT ELIGIBLE: no categorical match and income ${annual_income:.2f} > limit ${limit:.2f} for household_size={household_size}"

if __name__ == "__main__":
    # initialize and run the server
    mcp.run(transport='stdio')