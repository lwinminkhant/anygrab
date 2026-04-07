sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

curl -sS --resolve anygrab.lwinminkhant.pro:443:49.12.100.233 https://anygrab.lwinminkhant.pro/api/v1/health