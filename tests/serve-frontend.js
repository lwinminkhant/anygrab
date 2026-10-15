const { createServer } = require('node:http');
const { readFile } = require('node:fs/promises');
const { resolve, extname, sep } = require('node:path');

const root = resolve(__dirname, '../public');
const contentTypes = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.ttf': 'font/ttf',
};

// Local frontend test fixture; API responses are intercepted by Playwright.
createServer(async (request, response) => {
    try {
        const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
        const file = resolve(root, pathname === '/' ? 'index.html' : `.${pathname}`);
        if (!file.startsWith(root + sep)) {
            response.writeHead(403).end('Forbidden');
            return;
        }
        const body = await readFile(file);
        response.writeHead(200, {
            'Content-Type': contentTypes[extname(file)] || 'application/octet-stream',
            'Cache-Control': 'no-store',
        }).end(body);
    } catch (error) {
        response.writeHead(error.code === 'ENOENT' ? 404 : 400).end('File unavailable');
    }
}).listen(8765, '127.0.0.1');
