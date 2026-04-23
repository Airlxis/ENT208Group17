Set-Location -LiteralPath $PSScriptRoot
npx -y netlify-cli env:set DEEPSEEK_ENABLED true
npx -y netlify-cli deploy --prod --dir public --functions netlify/functions --message "Enable DeepSeek API"
