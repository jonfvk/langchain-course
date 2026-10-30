# Source this file to use the project's Node installation in the current shell.
PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
export PATH="$PROJECT_ROOT/.tools/node-v24.21.0-darwin-x64/bin:$PATH"
export npm_config_cache="$PROJECT_ROOT/.tools/npm-cache"
export npm_config_prefix="$PROJECT_ROOT/.tools/npm-global"
export npm_config_update_notifier=false
