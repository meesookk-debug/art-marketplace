from app import app

def test_routes_exist():
    routes = {r.rule for r in app.url_map.iter_rules()}
    assert '/' in routes
    assert '/login' in routes
    assert '/register' in routes
    assert '/dashboard' in routes
    assert '/admin/logs' in routes
