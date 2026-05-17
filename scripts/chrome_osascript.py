import json
import sys

from chrome_bridge import close_session, execute_js, open_url

def main():
    if len(sys.argv) < 2:
        print(json.dumps({'ok': False, 'error': 'No command provided'}))
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == 'open-url':
        if len(sys.argv) < 3:
            print(json.dumps({'ok': False, 'error': 'No URL provided'}))
            sys.exit(1)
        url = sys.argv[2]
        print(json.dumps(open_url(url)))
        
    elif cmd == 'execute-js':
        if len(sys.argv) < 3:
            print(json.dumps({'ok': False, 'error': 'No JS code provided'}))
            sys.exit(1)
        js_code_raw = sys.argv[2]
        print(json.dumps(execute_js(js_code_raw)))

    elif cmd == 'close-session':
        print(json.dumps(close_session()))
        
    else:
        print(json.dumps({'ok': False, 'error': f'Unknown command: {cmd}'}))
        sys.exit(1)

if __name__ == "__main__":
    main()
