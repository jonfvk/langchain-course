"""One sandbox OAuth diagnostic; no token-bearing public result, retries or MCP."""
import http.client
import json
import ssl
import certifi
from src.security.sandbox_keychain import load_sandbox
from src.security.oauth_request import build_refresh_request
from src.data.market_data.authentication import _parse_error_response, _exception_diagnostic


def diagnose_sandbox_oauth():
    return _diagnose_with_credentials(load_sandbox())


def _diagnose_with_credentials(credentials):
    result = {'http_status':None, 'oauth_succeeded':False}
    connection = None
    try:
        request = build_refresh_request('sandbox', credentials)
        connection = http.client.HTTPSConnection(request.host, timeout=30,
            context=ssl.create_default_context(cafile=certifi.where()))
        connection.set_debuglevel(0)
        connection.request(request.method, request.path, body=request.body, headers=request.headers)
        response = connection.getresponse()
        result['http_status'] = response.status
        raw = response.read(1024*1024+1)
        if not 200 <= response.status < 300:
            redactions = json.loads(request.body)
            redactions['client_id'] = credentials.client_id._for_auth()
            info = _parse_error_response(raw, response.getheader('Content-Type'), redactions)
            for key in ('error_code','error_message','content_type','response_characterization'):
                result[key] = info[key]
            # Read only already-sanitized diagnostic fields, never raw values.
            result['root_keys'] = info.get('root_keys', [])
            for field in info.get('json_structure', {}).get('fields', []):
                key = field.get('key')
                if key in ('error_description', 'message', 'detail', 'code', 'error') and 'sanitized_value' in field:
                    result[key] = field['sanitized_value']
            if result['error_message'] is None:
                result['error_message'] = result.get('error_description', result.get('message', result.get('detail')))
        elif len(raw) > 1024*1024:
            result['error_message'] = 'Response exceeded size limit'
        else:
            data = json.loads(raw)
            result['oauth_succeeded'] = (isinstance(data,dict) and
                isinstance(data.get('access_token'),str) and bool(data['access_token'].strip()))
            if not result['oauth_succeeded']:
                result['error_message'] = 'Response missing nonempty access token'
            del data
    except Exception as exc:
        result['error_message'] = _exception_diagnostic(exc)
    finally:
        if connection is not None: connection.close()
    return result
