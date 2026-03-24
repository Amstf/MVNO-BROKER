#!/bin/bash

# Step 1: Stop and remove old container if it exists
info "Cleaning up any previous 'oranslice-xapp' container..."
docker rm -f oranslice-xapp 2>/dev/null && success "Removed old container" || echo "No old container to remove"

# Step 2: Run container and execute hw_xapp_main directly
info "Running new xApp container with hw_xapp_main..."
docker run --rm --net host \
  -v /root/OAI-RIC-Network/xapp-oai/xapp_bs_connector/init:/opt/ric/config \
  --name oranslice-xapp oranslice-xapp \
  /root/OAI-RIC-Network/xapp-oai/xapp_bs_connector/hw_xapp_main

# Note: If hw_xapp_main is in a different path inside the container, adjust it accordingly.

success "Container exited (or crashed 😅), script finished."
