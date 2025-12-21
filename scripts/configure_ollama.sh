#!/bin/bash
set -e

echo "🔧 Configuring Ollama to run on internal port 11436..."

# Create systemd override directory if it doesn't exist
sudo mkdir -p /etc/systemd/system/ollama.service.d

# Create or update the override file
echo "📝 Writing override configuration..."
cat << EOF | sudo tee /etc/systemd/system/ollama.service.d/override.conf
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11436"
EOF

# Reload systemd and restart Ollama
echo "🔄 Reloading systemd and restarting Ollama..."
sudo systemctl daemon-reload
sudo systemctl restart ollama

echo "✅ Ollama is now running on port 11436."
echo "🛡️  You can now start Ollama Guardian on port 11434."
