import json
import sys

from chrome_bridge import close_session, execute_js, open_url

def main():
    if len(sys.argv) < 3:
        sys.exit(1)
    
    tool = sys.argv[1]
    args_raw = sys.argv[2]
    try:
        args = json.loads(args_raw)
    except:
        args = {}
    
    if tool == 'chrome_navigate':
        url = args.get('url', '')
        result = open_url(url, args.get('session'))
        if not result.get('ok'):
            print(f"Error: {result.get('error', 'unknown error')}", file=sys.stderr)
            sys.exit(1)
        print("OK")
    elif tool == 'chrome_javascript':
        code = args.get('code', '')
        result = execute_js(code, args.get('session'))
        if not result.get('ok'):
            print(f"Error: {result.get('error', 'unknown error')}", file=sys.stderr)
            sys.exit(1)
        res = result.get('result', '')
        if not res or res == 'missing value':
            print("")
            return
        
        # osascript often wraps string results in double quotes and escapes internal quotes
        if res.startswith('"') and res.endswith('"'):
            # Simple unescape for common cases
            res = res[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        print(res)
    elif tool == 'chrome_close_session':
        result = close_session(args.get('session'))
        if not result.get('ok'):
            print(f"Error: {result.get('error', 'unknown error')}", file=sys.stderr)
            sys.exit(1)
        print('OK')
    else:
        print(f"Unknown tool: {tool}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
