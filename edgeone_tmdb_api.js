var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// EdgeOne 使用标准 addEventListener 监听 fetch 事件
addEventListener("fetch", (event) => {
  event.respondWith(handleRequest(event.request));
});

async function handleRequest(request) {
  const url = new URL(request.url);
  const pathname = url.pathname;
  const search = url.search;
  
  const corsHeaders = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-API-Key",
    "Cross-Origin-Resource-Policy": "cross-origin"
  };

  function rewriteConfigImages(payload) {
    if (!payload || typeof payload !== "object" || !payload.images)
      return payload;
    const origin = url.origin.replace(/\/$/, "");
    const proxyBase = `${origin}/t/p/`;
    payload.images.base_url = proxyBase;
    payload.images.secure_base_url = proxyBase;
    return payload;
  }
  __name(rewriteConfigImages, "rewriteConfigImages");

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 200, headers: corsHeaders });
  }

  const API_KEY = request.headers.get("X-API-Key") || url.searchParams.get("api_key") || url.searchParams.get("key");
  const userAgent = request.headers.get("User-Agent") || "";
  
  // EdgeOne 获取客户端真实 IP 的标准请求头是 X-Forwarded-For 或通过 request.eo 获取
  const clientIP = request.headers.get("X-Forwarded-For")?.split(',')[0].trim() || "unknown";
  
  // EdgeOne 对应的地理位置信息在 request.eo.geo 中
  const country = request.eo?.geo?.countryCode || "unknown";

  const suspiciousUA = ["curl", "wget", "python", "scrapy", "spider"];
  const isSuspicious = suspiciousUA.some((ua) => userAgent.toLowerCase().includes(ua));
  
  if (userAgent.toLowerCase().includes("bot") && !userAgent.includes("googlebot") || isSuspicious && !userAgent.includes("Mozilla")) {
    return new Response(getFake404HTML(), { status: 404, headers: { "Content-Type": "text/html", ...corsHeaders } });
  }

  const blockedCountries = [];
  if (blockedCountries.includes(country)) {
    return new Response(getFake404HTML(), { status: 404, headers: { "Content-Type": "text/html", ...corsHeaders } });
  }

  if (pathname === "/admin/status" && API_KEY && API_KEY.length === 32) {
    return new Response(JSON.stringify({
      status: "active",
      version: "2.0.0",
      endpoints: { images: "/t/p/{size}/{path}", api: "/3/{endpoint}" },
      client_info: { ip: clientIP, country, ua: userAgent.substring(0, 50) },
      security: { api_key_provided: true, request_secure: true },
      performance: { cache_enabled: true, compression: true },
      timestamp: (new Date()).toISOString()
    }), {
      headers: { "Content-Type": "application/json", ...corsHeaders }
    });
  }

  if (pathname === "/health" || pathname === "/ping") {
    return new Response(JSON.stringify({
      status: "ok",
      timestamp: (new Date()).toISOString(),
      uptime: "active"
    }), {
      headers: { "Content-Type": "application/json", ...corsHeaders }
    });
  }

  if (pathname === "/" || pathname === "") {
    return new Response(getFake404HTML(), {
      status: 404,
      headers: { "Content-Type": "text/html; charset=utf-8", ...corsHeaders }
    });
  }

  // 0. 头像代理部分（Gravatar）
  if (pathname.startsWith("/avatar/")) {
    const avatarHash = pathname.replace("/avatar/", "").split("/")[0];
    if (!avatarHash || avatarHash.length < 10) {
      return new Response(getFake404HTML(), {
        status: 404,
        headers: { "Content-Type": "text/html; charset=utf-8", ...corsHeaders }
      });
    }
    try {
      const avatarUrl = `https://www.gravatar.com/avatar/${avatarHash}?d=identicon&s=80`;
      const response = await fetch(avatarUrl, {
        headers: {
          "User-Agent": "Mozilla/5.0 (compatible; TMDB-Proxy/1.0)",
          "Accept": "image/*"
        }
      });

      if (!response.ok) {
        return new Response(getFake404HTML(), {
          status: 404,
          headers: { "Content-Type": "text/html; charset=utf-8", ...corsHeaders }
        });
      }

      return new Response(response.body, {
        status: response.status,
        headers: {
          "Content-Type": response.headers.get("Content-Type") || "image/png",
          "Cache-Control": "public, max-age=86400, immutable",
          "ETag": response.headers.get("ETag") || "",
          "Last-Modified": response.headers.get("Last-Modified") || "",
          "Content-Length": response.headers.get("Content-Length") || "",
          "Vary": "Accept-Encoding",
          ...corsHeaders
        }
      });
    } catch (error) {
      return new Response(getFake404HTML(), {
        status: 502,
        headers: { "Content-Type": "text/html; charset=utf-8", ...corsHeaders }
      });
    }
  }

  // 1. 图片代理部分（海报/背景/演员头像 + TMDB 官方资源如 Logo SVG）
  if (pathname.startsWith("/t/p/") || pathname.startsWith("/assets/")) {
    try {
      const originHost = pathname.startsWith("/assets/")
        ? "https://www.themoviedb.org"
        : "https://image.tmdb.org";
      const imageUrl = `${originHost}${pathname}`;
      
      // 注意：EdgeOne 默认遵循源站的缓存策略。
      // 如果要强制在 EdgeOne 节点缓存图片，推荐在返回的 Response 中设置 Cache-Control 强缓存，
      // 或者在 EdgeOne 控制台的“规则引擎”中为 /t/p/* 配置缓存时间（7天）。
      const response = await fetch(imageUrl, {
        headers: {
          "User-Agent": "Mozilla/5.0 (compatible; TMDB-Proxy/1.0)",
          "Accept": "image/*"
        }
      });

      if (!response.ok) {
        return new Response(getFake404HTML(), {
          status: 404,
          headers: { "Content-Type": "text/html; charset=utf-8", ...corsHeaders }
        });
      }

      // 组装返回给客户端的图片
      return new Response(response.body, {
        status: response.status,
        headers: {
          "Content-Type": response.headers.get("Content-Type") || "image/jpeg",
          // 在此处加入强缓存响应头，EdgeOne 节点和浏览器均会缓存该图片 7 天
          "Cache-Control": "public, max-age=604800, immutable",
          "ETag": response.headers.get("ETag") || "",
          "Last-Modified": response.headers.get("Last-Modified") || "",
          "Content-Length": response.headers.get("Content-Length") || "",
          "Vary": "Accept-Encoding",
          ...corsHeaders
        }
      });
    } catch (error) {
      return new Response(getFake404HTML(), {
        status: 404,
        headers: { "Content-Type": "text/html; charset=utf-8", ...corsHeaders }
      });
    }
  }

  // 2. API 代理部分
  if (pathname.startsWith("/3/")) {
    if (!API_KEY) {
      return new Response(getFake404HTML(), {
        status: 404,
        headers: { "Content-Type": "text/html; charset=utf-8", ...corsHeaders }
      });
    }
    try {
      let apiUrl = `https://api.tmdb.org${pathname}${search}`;
      if (!search.includes("api_key=")) {
        const separator = search ? "&" : "?";
        apiUrl += `${separator}api_key=${API_KEY}`;
      }

      // 【核心修复】：显式构建标准的、干净的请求头，避免携带原请求中带有你域名的 Host
      const forwardHeaders = new Headers();
      forwardHeaders.set("Host", "api.tmdb.org");
      forwardHeaders.set("Accept", "application/json");
      forwardHeaders.set("User-Agent", request.headers.get("User-Agent") || "Mozilla/5.0");

      // 透传 Authorization 头，支持 Bearer Token 认证（watchlist/账号接口必需）
      if (request.headers.has("Authorization")) {
          forwardHeaders.set("Authorization", request.headers.get("Authorization"));
      }
      
      // 注意：暂时移除过多的 Accept-Encoding，让 EdgeOne 自动处理压缩，防止 text() 解析乱码
      if (request.headers.has("Accept-Language")) {
        forwardHeaders.set("Accept-Language", request.headers.get("Accept-Language"));
      }

      const response = await fetch(apiUrl, {
        method: request.method,
        headers: forwardHeaders
      });

      // 检查源站是否返回错误
      if (!response.ok) {
        return new Response(JSON.stringify({ error: `TMDB origin error: ${response.status}` }), {
          status: response.status,
          headers: { "Content-Type": "application/json", ...corsHeaders }
        });
      }

      let responseText = await response.text();
      
      const cacheTime = pathname.includes("configuration") ? 3600 : (
        pathname.includes("search") ? 300 : (
          pathname.includes("popular") ? 1800 : 600
        )
      );

      if (pathname.startsWith("/3/configuration")) {
        try {
          const json = JSON.parse(responseText);
          responseText = JSON.stringify(rewriteConfigImages(json));
        } catch (err) {
          // 打印错误日志便于在 EdgeOne 控制台调试
          console.error("Failed to parse configuration JSON:", err);
        }
      }

      // 移除可能冲突的底层编码头，让 EdgeOne 统一重新打包
      const responseHeaders = new Headers(corsHeaders);
      responseHeaders.set("Content-Type", "application/json; charset=utf-8");
      responseHeaders.set("Cache-Control", `public, max-age=${cacheTime}`);

      return new Response(responseText, {
        status: response.status,
        headers: responseHeaders
      });
    } catch (error) {
      // 捕捉具体错误返回，方便你排查究竟是 fetch 失败还是 JSON 解析失败
      return new Response(JSON.stringify({ 
        error: "Service temporarily unavailable", 
        details: error.message 
      }), {
        status: 503,
        headers: { "Content-Type": "application/json", ...corsHeaders }
      });
    }
  }

  // 3. Google Fonts CSS 代理 (fonts.googleapis.com)
  // 路径格式: /fonts/css/css2?family=...
  if (pathname.startsWith("/fonts/css/")) {
    try {
      // 提取 Google Fonts 的路径部分
      const fontsPath = pathname.replace("/fonts/css/", "");
      const fontsUrl = `https://fonts.googleapis.com/${fontsPath}${search}`;
      
      const response = await fetch(fontsUrl, {
        headers: {
          "User-Agent": request.headers.get("User-Agent") || "Mozilla/5.0",
          "Accept": "text/css,*/*;q=0.1"
        }
      });

      if (!response.ok) {
        return new Response(`/* Google Fonts CSS error: ${response.status} */`, {
          status: response.status,
          headers: { "Content-Type": "text/css", ...corsHeaders }
        });
      }

      let cssText = await response.text();
      
      // 重写 CSS 中的 fonts.gstatic.com URL 为相对路径
      // 将 https://fonts.gstatic.com/... 替换为 /fonts/gstatic/...
      cssText = cssText.replace(/https:\/\/fonts\.gstatic\.com\//g, "/fonts/gstatic/");

      const responseHeaders = new Headers(corsHeaders);
      responseHeaders.set("Content-Type", "text/css; charset=utf-8");
      responseHeaders.set("Cache-Control", "public, max-age=86400"); // 1 天缓存

      return new Response(cssText, {
        status: response.status,
        headers: responseHeaders
      });
    } catch (error) {
      return new Response(`/* Google Fonts proxy error: ${error.message} */`, {
        status: 502,
        headers: { "Content-Type": "text/css", ...corsHeaders }
      });
    }
  }

  // 4. Google Fonts 字体文件代理 (fonts.gstatic.com)
  // 路径格式: /fonts/gstatic/s/...woff2
  if (pathname.startsWith("/fonts/gstatic/")) {
    try {
      // 提取 gstatic 的路径部分
      const gstaticPath = pathname.replace("/fonts/gstatic/", "");
      const fontUrl = `https://fonts.gstatic.com/${gstaticPath}`;
      
      const response = await fetch(fontUrl, {
        headers: {
          "User-Agent": request.headers.get("User-Agent") || "Mozilla/5.0",
          "Accept": "font/woff2,*/*;q=0.1",
          "Origin": "https://fonts.googleapis.com"
        }
      });

      if (!response.ok) {
        return new Response("/* Font file not found */", {
          status: response.status,
          headers: { "Content-Type": "text/plain", ...corsHeaders }
        });
      }

      const responseHeaders = new Headers(corsHeaders);
      responseHeaders.set("Content-Type", response.headers.get("Content-Type") || "font/woff2");
      responseHeaders.set("Cache-Control", "public, max-age=31536000, immutable"); // 1 年缓存
      responseHeaders.set("Cross-Origin-Resource-Policy", "cross-origin");

      return new Response(response.body, {
        status: response.status,
        headers: responseHeaders
      });
    } catch (error) {
      return new Response("/* Font proxy error */", {
        status: 502,
        headers: { "Content-Type": "text/plain", ...corsHeaders }
      });
    }
  }

  return new Response(getFake404HTML(), {
    status: 404,
    headers: { "Content-Type": "text/html; charset=utf-8", ...corsHeaders }
  });
}

function getFake404HTML() {
  return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>404 Not Found</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f8f9fa; color: #212529; line-height: 1.6; min-height: 100vh;
            display: flex; align-items: center; justify-content: center;
        }
        .error-container { text-align: center; max-width: 600px; padding: 2rem; }
        .error-code { font-size: 8rem; font-weight: 300; color: #6c757d; margin-bottom: 1rem; line-height: 1; }
        .error-title { font-size: 2rem; font-weight: 400; color: #495057; margin-bottom: 1rem; }
        .error-message { font-size: 1.1rem; color: #6c757d; margin-bottom: 2rem; }
        .error-details {
            background: #e9ecef; border-radius: 8px; padding: 1rem; margin: 1.5rem 0;
            font-family: 'Courier New', monospace; font-size: 0.9rem; color: #495057; text-align: left;
        }
        .back-link {
            display: inline-block; padding: 0.75rem 1.5rem; background: #007bff; color: white;
            text-decoration: none; border-radius: 4px; transition: background-color 0.2s;
        }
        .back-link:hover { background: #0056b3; }
        .footer { margin-top: 3rem; font-size: 0.9rem; color: #adb5bd; }
        .server-info { margin-top: 1rem; font-size: 0.8rem; color: #ced4da; }
    </style>
</head>
<body>
    <div class="error-container">
        <div class="error-code">404</div>
        <h1 class="error-title">Page Not Found</h1>
        <p class="error-message">The requested resource could not be found on this server.</p>

        <div class="error-details">
            <strong>Error Details:</strong><br>
            \u2022 Request Method: GET<br>
            \u2022 Request URL: ${new Date().toISOString().split("T")[0]}<br>
            \u2022 Server: Tencent Cloud EdgeOne<br>
            \u2022 Timestamp: ${new Date().toISOString()}
        </div>

        <p style="color: #6c757d; margin: 1.5rem 0;">
            If you believe this is an error, please contact the site administrator.
        </p>

        <a href="javascript:history.back()" class="back-link">\u2190 Go Back</a>

        <div class="footer">
            <p>This page was generated automatically.</p>
            <div class="server-info">Server: EdgeOne Functions | Error Code: HTTP_404_NOT_FOUND</div>
        </div>
    </div>

    <script>
        console.log('%c\u{1F3AC} TMDB Proxy Service v2.0 (EdgeOne)', 'color: #007bff; font-size: 16px; font-weight: bold;');
        console.log('%cService Status: \u2705 Active (Enhanced)', 'color: #28a745;');
        console.log('%cEndpoints:', 'color: #6c757d;');
        console.log('  \u2022 Images: /t/p/{size}/{path} (7-day cache)');
        console.log('  \u2022 Assets: /assets/{path} (TMDB logos, etc.)');
        console.log('  \u2022 Avatar: /avatar/{hash} (1-day cache)');
        console.log('  \u2022 API: /3/{endpoint} (Smart cache 5min-1hr)');
        console.log('  \u2022 Health: /health, /ping');
        console.log('  \u2022 Admin: /admin/status (requires API key)');
        console.log('%cAPI Key Methods:', 'color: #17a2b8;');
        console.log('  \u2022 Header: X-API-Key: your_api_key');
        console.log('  \u2022 URL Param: ?api_key=your_api_key');
        console.log('  \u2022 URL Param: ?key=your_api_key');
        console.log('%cFeatures: Cache, Compression, Security, Geo-blocking', 'color: #28a745;');
        console.log('%c\u26A0\uFE0F Disguised as 404 for security', 'color: #ffc107;');

        window.testAPI = () => fetch('/3/configuration').then(r=>r.json()).then(console.log);
        window.testImage = () => { const i=new Image(); i.onload=()=>console.log('\u2705 Image OK'); i.onerror=()=>console.log('\u274C Image failed'); i.src='/t/p/w500/bcP7FtskwsNp1ikpMQJzDPjofP5.jpg'; };
        console.log('%cTest: testAPI() | testImage()', 'color: #17a2b8;');
    <\/script>
</body>
</html>`;
}
__name(getFake404HTML, "getFake404HTML");