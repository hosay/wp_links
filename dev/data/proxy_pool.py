"""Pool of 50 Rayobyte residential proxies for rotation.

When an account's assigned proxy fails, the orchestrator tries proxies
from this pool until one connects successfully, then persists the
working proxy to the account's DB record.
"""

import random

# Pre-parsed proxy configs matching the format build_proxy() expects.
# Each dict: {"country": "XX", "region": "...", "city": "..."}
PROXY_POOL = [
    # MEXICO (10 cities)
    {"country": "MX", "region": "jalisco", "city": "guadalajara"},
    {"country": "MX", "region": "mexico_city", "city": "cuauhtémoc"},
    {"country": "MX", "region": "baja_california", "city": "tijuana"},
    {"country": "MX", "region": "puebla", "city": "puebla_city"},
    {"country": "MX", "region": "chihuahua", "city": "chihuahua_city"},
    {"country": "MX", "region": "guanajuato", "city": "león"},
    {"country": "MX", "region": "tamaulipas", "city": "tampico"},
    {"country": "MX", "region": "guerrero", "city": "acapulco_de_juárez"},
    {"country": "MX", "region": "veracruz", "city": "xalapa"},
    {"country": "MX", "region": "jalisco", "city": "zapopan"},
    # SPAIN (7 cities)
    {"country": "ES", "region": "catalonia", "city": "barcelona"},
    {"country": "ES", "region": "andalusia", "city": "seville"},
    {"country": "ES", "region": "aragon", "city": "zaragoza"},
    {"country": "ES", "region": "andalusia", "city": "málaga"},
    {"country": "ES", "region": "basque_country", "city": "bilbao"},
    {"country": "ES", "region": "valencia", "city": "alicante"},
    {"country": "ES", "region": "principality_of_asturias", "city": "gijón"},
    # COLOMBIA (5 cities)
    {"country": "CO", "region": "bogota_d.c.", "city": "bogotá"},
    {"country": "CO", "region": "antioquia", "city": "medellín"},
    {"country": "CO", "region": "valle_del_cauca_department", "city": "cali"},
    {"country": "CO", "region": "cundinamarca", "city": "soacha"},
    {"country": "CO", "region": "antioquia", "city": "bello"},
    # ARGENTINA (5 cities)
    {"country": "AR", "region": "buenos_aires_f.d.", "city": "buenos_aires"},
    {"country": "AR", "region": "cordoba", "city": "villa_maría"},
    {"country": "AR", "region": "santa_fe", "city": "rosario"},
    {"country": "AR", "region": "mendoza", "city": "mendoza"},
    {"country": "AR", "region": "tucuman", "city": "san_miguel_de_tucumán"},
    # PERU (3 cities)
    {"country": "PE", "region": "lima_region", "city": "san_juan_de_lurigancho"},
    {"country": "PE", "region": "arequipa", "city": "arequipa"},
    {"country": "PE", "region": "lima_region", "city": "huaral"},
    # CHILE (3 cities)
    {"country": "CL", "region": "santiago_metropolitan", "city": "santiago"},
    {"country": "CL", "region": "valparaiso", "city": "valparaíso"},
    {"country": "CL", "region": "santiago_metropolitan", "city": "melipilla"},
    # ECUADOR (3 cities)
    {"country": "EC", "region": "pichincha", "city": "quito"},
    {"country": "EC", "region": "guayas", "city": "guayaquil"},
    {"country": "EC", "region": "pichincha", "city": "cayambe"},
    # BOLIVIA (2 cities)
    {"country": "BO", "region": "santa_cruz_department", "city": "santa_cruz_de_la_sierra"},
    {"country": "BO", "region": "departamento_de_cochabamba", "city": "cochabamba"},
    # PARAGUAY (2 cities)
    {"country": "PY", "region": "central_department", "city": "luque"},
    {"country": "PY", "region": "cordillera_department", "city": "caacupé"},
    # URUGUAY (1 city)
    {"country": "UY", "region": "montevideo_department", "city": "montevideo"},
    # GUATEMALA (1 city)
    {"country": "GT", "region": "guatemala", "city": "guatemala_city"},
    # DOMINICAN REPUBLIC (2 cities)
    {"country": "DO", "region": "nacional", "city": "santo_domingo"},
    {"country": "DO", "region": "santiago_province", "city": "santiago_de_los_caballeros"},
    # HONDURAS (2 cities)
    {"country": "HN", "region": "yoro_department", "city": "el_progreso"},
    {"country": "HN", "region": "comayagua_department", "city": "comayagua"},
    # EL SALVADOR (1 city)
    {"country": "SV", "region": "san_salvador_department", "city": "san_salvador"},
    # NICARAGUA (1 city)
    {"country": "NI", "region": "managua_department", "city": "managua"},
    # COSTA RICA (2 cities)
    {"country": "CR", "region": "alajuela_province", "city": "alajuela"},
    {"country": "CR", "region": "heredia_province", "city": "heredia"},
]


def get_rotation_pool(current_proxy: dict, max_attempts: int = 10) -> list[dict]:
    """Build a proxy rotation list: current proxy first, then shuffled pool entries.

    Args:
        current_proxy: The account's currently assigned proxy config.
            If empty dict, only pool proxies are returned.
        max_attempts: Maximum number of proxies to return.

    Returns:
        List of proxy config dicts, up to max_attempts long.
        Current proxy is always first (if non-empty).
    """
    # Shuffle a copy of the pool
    shuffled = random.sample(PROXY_POOL, len(PROXY_POOL))

    if not current_proxy:
        return shuffled[:max_attempts]

    # Deduplicate: remove current proxy from shuffled pool
    filtered = [p for p in shuffled if p != current_proxy]

    # Current proxy first, then fill remaining slots from pool
    result = [current_proxy] + filtered
    return result[:max_attempts]
