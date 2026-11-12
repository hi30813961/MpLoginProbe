// MpLoginProbe v5 — 公众号后台登录态 / 会话快照探针 + 本地 CDP 控制通道
//                   + 内置「本地素材库 · 流水线」管理台（第二个 WebView2）
// Qt 6.5.3 MinGW + WebView2 COM，动态加载 WebView2Loader.dll（无需 MSVC / QtWebEngine）
// 复用 office2pdf2winformcpp_qt6/src/ui/WebViewPanel.cpp 的初始化模式。
// 只读探针：不重放私有接口、不做发布/删除/保存等写操作。
//
// v5 新增：内嵌 CPython，右侧管理台服务与 Qt 同进程
//   运行时来自随 exe 分发的 pyruntime/（python313.dll + 标准库 zip + DLLs），
//   由本进程 LoadLibraryW 动态加载，独立 std::thread 里 Py_Initialize + 跑
//   pipeline/_embedded_boot.py，不再 fork python.exe。换机无需安装 Python。
//   _embedded_boot.py 把 ui_server.run_cli 改为「当前解释器内调用 pipeline.main」，
//   因为内嵌后 sys.executable 是 MpLoginProbe.exe 自己，原子进程路径会去启动本程序。
//   内嵌失败（缺 pyruntime / 初始化异常）自动回退 v4 的 QProcess 拉起外部 Python。
//   退出靠文件哨兵 .py_stop：Python 侧 srv.shutdown() → Py_FinalizeEx() → join。
//
// v4：主窗口用 QSplitter 左右并排放【两个互不干扰的 WebView2】
//   左：mp.weixin.qq.com（登录态 / CDP 8765 通道，始终可见，保持渲染）
//   右：http://127.0.0.1:8766/（本地管理台，独立 MpAdminProfile，不共享登录 cookie）
//   启动时会先探测 8766 端口：已有服务直接复用；端口就绪后自动导航。
//   为什么不能用「同一个 WebView2 来回切页」：8765 的 CDP 注入、登录态、SPA 页面
//   都绑定在左侧那个 WebView2 上，切走了 pipeline 的存草稿/改稿/补图会全部失灵。
//
// v3 新增：127.0.0.1:8765 的本地 HTTP 控制通道（详见 startCdpHttp）：
//   /screenshot  Page.captureScreenshot   /click  Input.dispatchMouseEvent
//   /text        Input.insertText         /key    Input.dispatchKeyEvent
//   /metrics     Page.getLayoutMetrics    /cdp    通用透传   /status 状态
// 使外部脚本可在窗口最小化 / 被遮挡时操作页面，且完全不占用物理鼠标。
// 配套 Python 工具：vision_click.py（屏幕后端）、cdp_vision.py（CDP 后端）。
// 安全：只监听回环地址，切勿把 8765 映射或转发到局域网。
//
// 外部可读文件（与 exe 同目录）：
//   status.txt            state / token / fingerprint / url / 计数 / time
//   events.log            导航与 /cgi-bin/ 响应摘要
//   nav_history.txt       每次 URL 变化（含 SPA 栏目切换）
//   session.json          会话快照：页面侧 cookie / localStorage / sessionStorage
//                         + CDP 拿到的全部 Cookie（含 HttpOnly）+ 接口命中统计
//   cookies.txt           Cookie 明细表（人类可读）
//   cookie-header.txt     可直接用作 Cookie 请求头的一行
//   responses/all.jsonl   全部 /cgi-bin/ 响应的完整原文（逐行 JSON）
//   responses/<api>.json  各业务接口最近一次完整响应
//
// 注意：session.json / cookie-header.txt 含明文登录凭据，勿外传、勿提交仓库。

#include <windows.h>
#include <objbase.h>
#include "WebView2.h"

#include <QApplication>
#include <QScreen>
#include <QWidget>
#include <QLabel>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QVBoxLayout>
#include <QHBoxLayout>
#include <QUrl>
#include <QUrlQuery>
#include <QFile>
#include <QTextStream>
#include <QStringConverter>
#include <QDateTime>
#include <QDir>
#include <QTimer>
#include <QMetaObject>
#include <QCoreApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QJsonValue>
#include <QJsonParseError>
#include <QStringList>
#include <QMap>
#include <QHash>
#include <QPointer>
#include <QProcess>
#include <QProcessEnvironment>
#include <QSplitter>
#include <QCloseEvent>
#include <QDesktopServices>
#include <QTcpServer>
#include <QTcpSocket>
#include <QHostAddress>
#include <QThread>
#include <functional>
#include <thread>
#include <atomic>
#include <cstdio>

struct Ctx {
    QWidget*        host   = nullptr;
    QLabel*         status = nullptr;
    QPlainTextEdit* log    = nullptr;
    HWND            hwnd   = nullptr;
    HMODULE         loader = nullptr;
    ICoreWebView2Environment* env  = nullptr;
    ICoreWebView2Controller*  ctrl = nullptr;
    ICoreWebView2*            web  = nullptr;
    EventRegistrationToken navTok{};
    EventRegistrationToken msgTok{};
    EventRegistrationToken srcTok{};
    QString state       = QStringLiteral("STARTING");
    QString token;
    QString fingerprint;
    QString url;
    int     apiCount    = 0;   // 捕获到的业务接口响应次数
    int     cookieCount = 0;
    QMap<QString, int> apiHits;
};
static Ctx g;

// ── 内置管理台（第二个 WebView2，与登录态探针完全隔离）────────────────
struct AdminCtx {
    QWidget*  pane   = nullptr;   // QSplitter 右侧容器
    QWidget*  host   = nullptr;   // 承载管理台 WebView2 的 HWND
    QLabel*   status = nullptr;
    QSplitter* split = nullptr;
    HWND      hwnd   = nullptr;
    ICoreWebView2Environment* env  = nullptr;
    ICoreWebView2Controller*  ctrl = nullptr;
    ICoreWebView2*            web  = nullptr;
    bool      ready      = false;
    bool      wide       = false;
    QProcess* proc       = nullptr;   // 非空 = 这个进程是我们自己拉起的，退出时才回收
    int       waitTicks  = 0;
    QStringList pyCandidates;
    int       pyIndex    = 0;
    bool      embedFallbackTried = false;   // 内嵌失败只回退一次外部 Python
    bool      embedded   = false;           // 当前 8766 由本进程内嵌 CPython 提供
};
static AdminCtx a;
static const quint16 kAdminPort = 8766;
static const wchar_t* kAdminUrl = L"http://127.0.0.1:8766/";

// ── 内嵌 CPython（v5）──────────────────────────────────────────────
// 0=未启动 1=初始化中 2=服务线程运行中 3=失败（可回退 QProcess） 4=已停止
enum { EMB_IDLE = 0, EMB_STARTING = 1, EMB_RUNNING = 2, EMB_FAILED = 3, EMB_STOPPED = 4 };
static std::atomic<int>  g_embedState{EMB_IDLE};
static std::atomic<bool> g_embedDone{false};
struct EmbedThread {
    std::thread t;
    // 静态对象析构时若还 joinable 会 std::terminate，这里兜底 detach
    ~EmbedThread() { if (t.joinable()) t.detach(); }
};
static EmbedThread g_embedThread;

static QString baseDir();   // 定义在下方，这里先声明

static QString embedRoot()    { return baseDir() + QStringLiteral("/pyruntime"); }
static QString embedBoot()    { return baseDir() + QStringLiteral("/pipeline/_embedded_boot.py"); }
static QString embedStopFile(){ return baseDir() + QStringLiteral("/.py_stop"); }
static QString embedLogFile() { return baseDir() + QStringLiteral("/embed.log"); }

// 把路径写进环境变量，避免任何字符串转义/编码问题；Python 侧只认环境变量
static void setWideEnv(const wchar_t* key, const QString& value)
{
    SetEnvironmentVariableW(key, reinterpret_cast<const wchar_t*>(value.utf16()));
}
static bool startEmbeddedAdmin();
static void stopEmbeddedAdmin();

struct Snapshot {
    bool    active   = false;
    bool    pageDone = false;
    bool    cdpDone  = false;
    QString startedAt;
    QString pageRaw;   // ExecuteScript 返回值（JSON）
    QString cdpRaw;    // CDP Network.getAllCookies 返回值（JSON）
};
static Snapshot g_snap;

static QString baseDir() { return QCoreApplication::applicationDirPath(); }

static void writeText(const QString& path, const QString& s, bool append = false)
{
    QFile f(path);
    QIODevice::OpenMode mode = QIODevice::WriteOnly | QIODevice::Text;
    mode |= (append ? QIODevice::Append : QIODevice::Truncate);
    if (!f.open(mode)) return;
    QTextStream o(&f);
    o.setEncoding(QStringConverter::Utf8);
    o << s;
}

static void writeStatus()
{
    QString s;
    s += QStringLiteral("state=") + g.state + QStringLiteral("\n");
    s += QStringLiteral("token=") + g.token + QStringLiteral("\n");
    s += QStringLiteral("fingerprint=") + g.fingerprint + QStringLiteral("\n");
    s += QStringLiteral("url=") + g.url + QStringLiteral("\n");
    s += QStringLiteral("api_hits=") + QString::number(g.apiCount) + QStringLiteral("\n");
    s += QStringLiteral("cookie_count=") + QString::number(g.cookieCount) + QStringLiteral("\n");
    s += QStringLiteral("time=") + QDateTime::currentDateTime().toString(Qt::ISODate) + QStringLiteral("\n");
    writeText(baseDir() + QStringLiteral("/status.txt"), s);
}

static void logLine(const QString& s)
{
    const QString line = QDateTime::currentDateTime().toString(QStringLiteral("HH:mm:ss"))
                       + QStringLiteral("  ") + s;
    writeText(baseDir() + QStringLiteral("/events.log"), line + QStringLiteral("\n"), true);
    QMetaObject::invokeMethod(g.log, [line]() {
        if (g.log) g.log->appendPlainText(line);
    }, Qt::QueuedConnection);
}

static void setBanner(const QString& s, bool ok)
{
    QMetaObject::invokeMethod(g.status, [s, ok]() {
        if (!g.status) return;
        g.status->setText(s);
        g.status->setStyleSheet(ok
            ? QStringLiteral("background:#E8F5E9;color:#1B5E20;padding:10px;font-size:15px;font-weight:bold;")
            : QStringLiteral("background:#FFF3E0;color:#8A4B00;padding:10px;font-size:15px;font-weight:bold;"));
    }, Qt::QueuedConnection);
}

static void exportSession();     // 前向声明
static void finishSnapshot();   // 前向声明

// 记录每次导航 / SPA 栏目切换，顺便刷新 token 与 fingerprint
static void recordUrl(const QString& url)
{
    const QUrl  u(url);
    const QUrlQuery q(u);
    const QUrlQuery f(u.fragment());
    QString tok = q.queryItemValue(QStringLiteral("token"));
    if (tok.isEmpty()) tok = f.queryItemValue(QStringLiteral("token"));
    QString fp = q.queryItemValue(QStringLiteral("fingerprint"));
    if (fp.isEmpty()) fp = f.queryItemValue(QStringLiteral("fingerprint"));

    const bool changed = (url != g.url);
    g.url = url;
    if (!tok.isEmpty()) g.token = tok;
    if (!fp.isEmpty())  g.fingerprint = fp;

    const bool logged = !g.token.isEmpty() || url.contains(QStringLiteral("/cgi-bin/home"));
    const QString next = logged ? QStringLiteral("LOGGED_IN") : QStringLiteral("WAITING_LOGIN");
    const bool stateChanged = (next != g.state);
    g.state = next;

    if (changed) {
        writeText(baseDir() + QStringLiteral("/nav_history.txt"),
                  QDateTime::currentDateTime().toString(Qt::ISODate) + QStringLiteral("\t")
                  + url + QStringLiteral("\n"), true);
        logLine(QStringLiteral("栏目/页面 ") + url.left(220));
    }
    if (stateChanged) {
        logLine(logged ? (QStringLiteral("已登录，token=") + g.token)
                       : QStringLiteral("进入登录页，等待手机微信扫码"));
        setBanner(logged ? (QStringLiteral("已登录   token: ") + g.token)
                         : QStringLiteral("请用手机微信扫码登录公众号平台"), logged);
        if (logged) QTimer::singleShot(4000, qApp, []() { exportSession(); });
    }
    writeStatus();
}

// ── 完整响应落盘：业务接口存文件，噪声只留摘要 ──
static void onCaptured(const QString& msg)
{
    QJsonParseError pe{};
    const QJsonDocument d = QJsonDocument::fromJson(msg.toUtf8(), &pe);
    if (pe.error != QJsonParseError::NoError || !d.isObject()) {
        logLine(QStringLiteral("捕获响应(未解析) ") + msg.left(200));
        return;
    }
    const QJsonObject o = d.object();
    const QString url   = o.value(QStringLiteral("url")).toString();
    const QString body  = o.value(QStringLiteral("body")).toString();
    const int status    = o.value(QStringLiteral("status")).toInt();
    QString api = url.section(QLatin1Char('?'), 0, 0).section(QLatin1Char('/'), -1);
    if (api.isEmpty()) api = QStringLiteral("misc");

    // fingerprint 只出现在 XHR 参数里，不在地址栏，顺手补进会话状态
    const int fpAt = url.indexOf(QStringLiteral("fingerprint="));
    if (fpAt >= 0) {
        const QString fp = url.mid(fpAt + 12).section(QLatin1Char('&'), 0, 0);
        if (!fp.isEmpty() && fp != g.fingerprint) {
            g.fingerprint = fp;
            writeStatus();
            logLine(QStringLiteral("fingerprint=") + fp);
        }
    }

    const bool noise = url.contains(QStringLiteral("webreport"))
                    || url.contains(QStringLiteral("sysnotify"))
                    || url.contains(QStringLiteral("getnewmsgnum"));

    QDir().mkpath(baseDir() + QStringLiteral("/responses"));
    QJsonObject rec;
    rec.insert(QStringLiteral("time"), QDateTime::currentDateTime().toString(Qt::ISODate));
    rec.insert(QStringLiteral("api"), api);
    rec.insert(QStringLiteral("url"), url);
    rec.insert(QStringLiteral("status"), status);
    rec.insert(QStringLiteral("body"), body);
    const QString oneLine = QJsonDocument(rec).toJson(QJsonDocument::Compact);
    writeText(baseDir() + QStringLiteral("/responses/all.jsonl"), oneLine + QStringLiteral("\n"), true);
    if (!noise) {
        writeText(baseDir() + QStringLiteral("/responses/") + api + QStringLiteral(".json"),
                  QJsonDocument(rec).toJson(QJsonDocument::Indented));
        g.apiCount++;
        g.apiHits[api] = g.apiHits.value(api, 0) + 1;
        writeStatus();
    }
    // 从响应里摘出错误码，便于一眼看出会话是否还有效
    QString ret;
    const int p = body.indexOf(QStringLiteral("\"ret\":"));
    if (p >= 0) ret = body.mid(p, 14);
    logLine(QStringLiteral("[%1] %2B  HTTP %3  %4  %5")
                .arg(api, QString::number(body.size()), QString::number(status),
                     ret, noise ? QStringLiteral("(噪声)") : QString()));
}

// ── 页面创建时注入：旁观 fetch / XHR 响应，不做任何写操作 ──
static const char* kHookJs = R"JS(
(function(){
  if (window.__mpProbeHooked) return;
  window.__mpProbeHooked = true;
  var NOISE = /webreport|sysnotify|getnewmsgnum/;
  function send(kind, url, method, status, body){
    var limit = NOISE.test(url) ? 300 : 200000;
    try { window.chrome.webview.postMessage(JSON.stringify({
      kind:kind, url:String(url||''), method:String(method||''),
      status:status, body:String(body||'').slice(0,limit)
    })); } catch(e) {}
  }
  var _fetch = window.fetch;
  if (_fetch) {
    window.fetch = function(){
      var args = arguments;
      var url = (typeof args[0] === 'string') ? args[0] : ((args[0] && args[0].url) || '');
      var method = (args[1] && args[1].method) || 'GET';
      return _fetch.apply(this, args).then(function(res){
        if (/\/cgi-bin\//.test(url)) {
          try { res.clone().text().then(function(t){ send('fetch', url, method, res.status, t); }); } catch(e) {}
        }
        return res;
      });
    };
  }
  var _open = XMLHttpRequest.prototype.open;
  var _send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u){
    this.__mpUrl = u; this.__mpMethod = m;
    return _open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(){
    var self = this;
    this.addEventListener('loadend', function(){
      if (/\/cgi-bin\//.test(self.__mpUrl || '')) {
        var t = '';
        try { t = self.responseText || ''; } catch(e) {}
        send('xhr', self.__mpUrl, self.__mpMethod, self.status, t);
      }
    });
    return _send.apply(this, arguments);
  };
})();
)JS";

// 页面侧信息：直接返回对象，ExecuteScript 会 JSON 序列化
static const char* kPageJs = R"JS(
(function(){
  function dump(s){ var o={}; try{ for(var i=0;i<s.length;i++){ var k=s.key(i);
    o[k]=String(s.getItem(k)).slice(0,4000); } }catch(e){} return o; }
  var m = (location.href.match(/fingerprint=([0-9a-f]+)/i)||[])[1] || '';
  return {
    href: location.href,
    title: document.title,
    cookie: document.cookie,
    userAgent: navigator.userAgent,
    fingerprint: m,
    localStorage: dump(localStorage),
    sessionStorage: dump(sessionStorage)
  };
})()
)JS";

typedef HRESULT (STDMETHODCALLTYPE* CreateWebViewEnvFn)(
    PCWSTR, PCWSTR, ICoreWebView2EnvironmentOptions*,
    ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler*);

#define COM_BOILERPLATE(IFACE)                                                    \
public:                                                                           \
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&m_ref); } \
    ULONG STDMETHODCALLTYPE Release() override {                                  \
        LONG r = InterlockedDecrement(&m_ref);                                    \
        if (!r) { delete this; return 0; }                                       \
        return r;                                                                \
    }                                                                             \
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** ppv) override { \
        if (!ppv) return E_POINTER;                                              \
        if (riid == IID_IUnknown || riid == IID_##IFACE) {                       \
            *ppv = static_cast<IFACE*>(this); AddRef(); return S_OK;              \
        }                                                                         \
        *ppv = nullptr; return E_NOINTERFACE;                                    \
    }                                                                             \
private:                                                                          \
    LONG m_ref = 1;

class EnvReadyHandler : public ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler {
public:
    HRESULT STDMETHODCALLTYPE Invoke(HRESULT, ICoreWebView2Environment*) override;
    COM_BOILERPLATE(ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler)
};

class CtrlReadyHandler : public ICoreWebView2CreateCoreWebView2ControllerCompletedHandler {
public:
    HRESULT STDMETHODCALLTYPE Invoke(HRESULT, ICoreWebView2Controller*) override;
    COM_BOILERPLATE(ICoreWebView2CreateCoreWebView2ControllerCompletedHandler)
};

// 管理台专用的两套回调：不碰 g.*，不注入 hook，不写 nav_history / events.log
class AdminEnvReadyHandler : public ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler {
public:
    HRESULT STDMETHODCALLTYPE Invoke(HRESULT, ICoreWebView2Environment*) override;
    COM_BOILERPLATE(ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler)
};

class AdminCtrlReadyHandler : public ICoreWebView2CreateCoreWebView2ControllerCompletedHandler {
public:
    HRESULT STDMETHODCALLTYPE Invoke(HRESULT, ICoreWebView2Controller*) override;
    COM_BOILERPLATE(ICoreWebView2CreateCoreWebView2ControllerCompletedHandler)
};

static void ensureAdminService();
static void adminNavigate();

class NavCompletedHandler : public ICoreWebView2NavigationCompletedEventHandler {
public:
    HRESULT STDMETHODCALLTYPE Invoke(ICoreWebView2* sender,
                                     ICoreWebView2NavigationCompletedEventArgs* args) override
    {
        Q_UNUSED(args);
        LPWSTR raw = nullptr;
        if (sender && SUCCEEDED(sender->get_Source(&raw)) && raw) {
            const QString url = QString::fromWCharArray(raw);
            CoTaskMemFree(raw);
            QMetaObject::invokeMethod(qApp, [url]() { recordUrl(url); }, Qt::QueuedConnection);
        }
        return S_OK;
    }
    COM_BOILERPLATE(ICoreWebView2NavigationCompletedEventHandler)
};

class SourceChangedHandler : public ICoreWebView2SourceChangedEventHandler {
public:
    HRESULT STDMETHODCALLTYPE Invoke(ICoreWebView2* sender,
                                     ICoreWebView2SourceChangedEventArgs* args) override
    {
        Q_UNUSED(args);
        LPWSTR raw = nullptr;
        if (sender && SUCCEEDED(sender->get_Source(&raw)) && raw) {
            const QString url = QString::fromWCharArray(raw);
            CoTaskMemFree(raw);
            QMetaObject::invokeMethod(qApp, [url]() { recordUrl(url); }, Qt::QueuedConnection);
        }
        return S_OK;
    }
    COM_BOILERPLATE(ICoreWebView2SourceChangedEventHandler)
};

class WebMessageHandler : public ICoreWebView2WebMessageReceivedEventHandler {
public:
    HRESULT STDMETHODCALLTYPE Invoke(ICoreWebView2* sender,
                                     ICoreWebView2WebMessageReceivedEventArgs* args) override
    {
        Q_UNUSED(sender);
        LPWSTR raw = nullptr;
        if (args && SUCCEEDED(args->TryGetWebMessageAsString(&raw)) && raw) {
            const QString msg = QString::fromWCharArray(raw);
            CoTaskMemFree(raw);
            QMetaObject::invokeMethod(qApp, [msg]() { onCaptured(msg); }, Qt::QueuedConnection);
        }
        return S_OK;
    }
    COM_BOILERPLATE(ICoreWebView2WebMessageReceivedEventHandler)
};

class ScriptInjectedHandler : public ICoreWebView2AddScriptToExecuteOnDocumentCreatedCompletedHandler {
public:
    HRESULT STDMETHODCALLTYPE Invoke(HRESULT hr, LPCWSTR id) override
    {
        QMetaObject::invokeMethod(qApp, [hr]() {
            logLine(QStringLiteral("注入 hook ") + (SUCCEEDED(hr) ? QStringLiteral("成功")
                                                                 : QStringLiteral("失败 0x") +
                                                                   QString::number((ulong)hr, 16)));
        }, Qt::QueuedConnection);
        Q_UNUSED(id);
        return S_OK;
    }
    COM_BOILERPLATE(ICoreWebView2AddScriptToExecuteOnDocumentCreatedCompletedHandler)
};

// ── 会话快照：ExecuteScript 取页面侧状态，CDP 取含 HttpOnly 的全部 Cookie ──
class PageResultHandler : public ICoreWebView2ExecuteScriptCompletedHandler {
public:
    HRESULT STDMETHODCALLTYPE Invoke(HRESULT hr, LPCWSTR result) override
    {
        const QString s = result ? QString::fromWCharArray(result) : QString();
        QMetaObject::invokeMethod(qApp, [hr, s]() {
            if (!g_snap.active) return;
            g_snap.pageRaw = SUCCEEDED(hr) ? s : QStringLiteral("{\"error\":\"ExecuteScript failed\"}");
            g_snap.pageDone = true;
            finishSnapshot();
        }, Qt::QueuedConnection);
        return S_OK;
    }
    COM_BOILERPLATE(ICoreWebView2ExecuteScriptCompletedHandler)
};

class CdpHandler : public ICoreWebView2CallDevToolsProtocolMethodCompletedHandler {
public:
    HRESULT STDMETHODCALLTYPE Invoke(HRESULT hr, LPCWSTR result) override
    {
        const QString s = result ? QString::fromWCharArray(result) : QString();
        QMetaObject::invokeMethod(qApp, [hr, s]() {
            if (!g_snap.active) return;
            g_snap.cdpRaw = SUCCEEDED(hr) ? s : QStringLiteral("{\"cookies\":[]}");
            g_snap.cdpDone = true;
            finishSnapshot();
        }, Qt::QueuedConnection);
        return S_OK;
    }
    COM_BOILERPLATE(ICoreWebView2CallDevToolsProtocolMethodCompletedHandler)
};

static QJsonObject parseObject(const QString& raw)
{
    const QString t = raw.trimmed();
    if (t.isEmpty()) return QJsonObject();
    QJsonParseError pe{};
    const QJsonDocument d = QJsonDocument::fromJson(t.toUtf8(), &pe);
    if (pe.error == QJsonParseError::NoError && d.isObject()) return d.object();
    // 包成数组，兼容顶层是 JSON 字符串的情况（ExecuteScript 二次编码）
    QJsonParseError pe2{};
    const QJsonDocument arr = QJsonDocument::fromJson(
        (QStringLiteral("[") + t + QStringLiteral("]")).toUtf8(), &pe2);
    if (pe2.error == QJsonParseError::NoError && arr.isArray() && !arr.array().isEmpty()) {
        const QJsonValue v = arr.array().at(0);
        if (v.isObject()) return v.toObject();
        if (v.isString()) {
            QJsonParseError pe3{};
            const QJsonDocument inner = QJsonDocument::fromJson(v.toString().toUtf8(), &pe3);
            if (pe3.error == QJsonParseError::NoError && inner.isObject()) return inner.object();
        }
    }
    return QJsonObject();
}

static void finishSnapshot()
{
    if (!g_snap.active || !g_snap.pageDone || !g_snap.cdpDone) return;

    QJsonObject page  = parseObject(g_snap.pageRaw);
    QJsonObject cdp   = parseObject(g_snap.cdpRaw);
    QJsonArray  cookies = cdp.value(QStringLiteral("cookies")).toArray();
    if (cookies.isEmpty()) {
        // 兜底：页面侧 document.cookie（拿不到 HttpOnly，但至少有记录）
        const QString dc = page.value(QStringLiteral("cookie")).toString();
        for (const QString& kv : dc.split(QLatin1Char(';'), Qt::SkipEmptyParts)) {
            const QString t = kv.trimmed();
            const int p = t.indexOf(QLatin1Char('='));
            if (p <= 0) continue;
            QJsonObject c;
            c.insert(QStringLiteral("name"), t.left(p));
            c.insert(QStringLiteral("value"), t.mid(p + 1));
            c.insert(QStringLiteral("domain"), QStringLiteral("(document.cookie)"));
            c.insert(QStringLiteral("httpOnly"), false);
            cookies.append(c);
        }
    }

    QJsonObject apiObj;
    for (auto it = g.apiHits.constBegin(); it != g.apiHits.constEnd(); ++it)
        apiObj.insert(it.key(), it.value());

    QJsonObject root;
    root.insert(QStringLiteral("exported_at"), QDateTime::currentDateTime().toString(Qt::ISODate));
    root.insert(QStringLiteral("state"), g.state);
    root.insert(QStringLiteral("token"), g.token);
    root.insert(QStringLiteral("fingerprint"), g.fingerprint);
    root.insert(QStringLiteral("url"), g.url);
    root.insert(QStringLiteral("api_hits_total"), g.apiCount);
    root.insert(QStringLiteral("api_hits"), apiObj);
    root.insert(QStringLiteral("cookie_count"), cookies.size());
    root.insert(QStringLiteral("page"), page);
    root.insert(QStringLiteral("cookies"), cookies);
    root.insert(QStringLiteral("note"),
                QStringLiteral("只读快照。含明文凭据，勿外传、勿提交仓库；"
                               "本程序不重放私有接口，仅供本地核对登录态。"));
    writeText(baseDir() + QStringLiteral("/session.json"),
              QJsonDocument(root).toJson(QJsonDocument::Indented));

    QString table = QStringLiteral("# domain\tpath\tname\tvalue\thttpOnly\tsecure\tsession\texpires\n");
    QString header;
    for (const QJsonValue& v : cookies) {
        const QJsonObject c = v.toObject();
        const QString name = c.value(QStringLiteral("name")).toString();
        const QString val  = c.value(QStringLiteral("value")).toString();
        const QString dom  = c.value(QStringLiteral("domain")).toString();
        table += QStringLiteral("%1\t%2\t%3\t%4\t%5\t%6\t%7\t%8\n")
                    .arg(dom, c.value(QStringLiteral("path")).toString(), name, val,
                         c.value(QStringLiteral("httpOnly")).toBool() ? QStringLiteral("yes") : QStringLiteral("no"),
                         c.value(QStringLiteral("secure")).toBool()   ? QStringLiteral("yes") : QStringLiteral("no"),
                         c.value(QStringLiteral("session")).toBool()  ? QStringLiteral("yes") : QStringLiteral("no"),
                         QString::number(c.value(QStringLiteral("expires")).toDouble(), 'f', 0));
        if (dom.contains(QStringLiteral("weixin.qq.com")) || dom.isEmpty()
            || dom.contains(QStringLiteral("(document.cookie)")))
            header += name + QStringLiteral("=") + val + QStringLiteral("; ");
    }
    writeText(baseDir() + QStringLiteral("/cookies.txt"), table);
    writeText(baseDir() + QStringLiteral("/cookie-header.txt"), header.trimmed() + QStringLiteral("\n"));

    g.cookieCount = cookies.size();
    writeStatus();
    logLine(QStringLiteral("会话快照已导出：%1 个 Cookie（含 HttpOnly），%2 个接口，token=%3")
                .arg(QString::number(cookies.size()), QString::number(g.apiHits.size()), g.token));
    g_snap.active = false;
}

static void exportSession()
{
    if (!g.web) return;
    if (g_snap.active) return;
    g_snap = Snapshot{};
    g_snap.active = true;
    g_snap.startedAt = QDateTime::currentDateTime().toString(Qt::ISODate);
    const QString js = QString::fromUtf8(kPageJs);
    g.web->ExecuteScript(reinterpret_cast<LPCWSTR>(js.utf16()), new PageResultHandler);
    g.web->CallDevToolsProtocolMethod(L"Network.getAllCookies", L"{}", new CdpHandler);
    logLine(QStringLiteral("导出会话快照…"));
}

// ══════════════════════════════════════════════════════════════════════
// 本地 CDP 控制通道（仅 127.0.0.1:8765）—— 不截屏、不占鼠标、不需窗口前台
//   GET  /status       探针状态
//   GET  /metrics      页面 CSS 视口尺寸（把 PNG 像素坐标换算成 CDP 坐标）
//   GET  /screenshot   Page.captureScreenshot -> 直接返回 PNG 二进制
//   POST /click        Input.dispatchMouseEvent 三段（物理鼠标完全不动）
//   POST /text         Input.insertText（中文可直接插入）
//   POST /key          Input.dispatchKeyEvent（Enter / Ctrl+A / Backspace ...）
//   POST /cdp          通用 CDP 透传，便于扩展调试
// 安全：只监听回环地址。不要把该端口映射/转发到局域网。
// ══════════════════════════════════════════════════════════════════════
static const quint16 kCdpPort = 8765;
static QHash<QTcpSocket*, QByteArray> g_httpBuf;

using CdpCb = std::function<void(bool, const QJsonObject&)>;

// 通用 CDP 回调。注意：Invoke 返回后对象可能被 COM Release，
// 所以 lambda 只捕获回调副本与值，绝不捕获 this。
class CdpReplyHandler : public ICoreWebView2CallDevToolsProtocolMethodCompletedHandler {
public:
    explicit CdpReplyHandler(CdpCb f) : m_cb(std::move(f)) {}
    HRESULT STDMETHODCALLTYPE Invoke(HRESULT hr, LPCWSTR result) override
    {
        const QString s = result ? QString::fromWCharArray(result) : QString();
        const bool ok = SUCCEEDED(hr);
        CdpCb f = m_cb;
        QMetaObject::invokeMethod(qApp, [f, ok, s]() { f(ok, parseObject(s)); },
                                  Qt::QueuedConnection);
        return S_OK;
    }
    COM_BOILERPLATE(ICoreWebView2CallDevToolsProtocolMethodCompletedHandler)
private:
    CdpCb m_cb;
};

static void callCdp(const QString& method, const QJsonObject& params, CdpCb cb)
{
    if (!g.web) { cb(false, QJsonObject()); return; }
    const QString m = method;
    const QString p = QString::fromUtf8(QJsonDocument(params).toJson(QJsonDocument::Compact));
    g.web->CallDevToolsProtocolMethod(
        reinterpret_cast<LPCWSTR>(m.utf16()),
        reinterpret_cast<LPCWSTR>(p.utf16()),
        new CdpReplyHandler(std::move(cb)));
}

static void httpReply(QPointer<QTcpSocket> s, int code,
                      const QByteArray& type, const QByteArray& body)
{
    if (!s) return;
    const char* reason = (code == 200) ? "OK" : (code == 400 ? "Bad Request" : "Error");
    QByteArray h;
    h += "HTTP/1.1 " + QByteArray::number(code) + " " + reason + "\r\n";
    h += "Content-Type: " + type + "\r\n";
    h += "Content-Length: " + QByteArray::number(body.size()) + "\r\n";
    h += "Cache-Control: no-store\r\n";
    h += "Connection: close\r\n\r\n";
    s->write(h);
    if (!body.isEmpty()) s->write(body);
    s->flush();
    s->disconnectFromHost();
}

static void httpJson(QPointer<QTcpSocket> s, const QJsonObject& o, int code = 200)
{
    httpReply(s, code, "application/json; charset=utf-8",
              QJsonDocument(o).toJson(QJsonDocument::Compact));
}

static QJsonObject cdpOk(bool ok, const QString& method)
{
    QJsonObject o;
    o.insert(QStringLiteral("ok"), ok);
    o.insert(QStringLiteral("method"), method);
    return o;
}

static void handleHttp(QPointer<QTcpSocket> s, const QString& method,
                       const QString& path, const QJsonObject& body)
{
    const QString route = path.section(QLatin1Char('?'), 0, 0);

    if (route == QStringLiteral("/status")) {
        QJsonObject o;
        o.insert(QStringLiteral("ok"), true);
        o.insert(QStringLiteral("state"), g.state);
        o.insert(QStringLiteral("token"), g.token);
        o.insert(QStringLiteral("url"), g.url);
        o.insert(QStringLiteral("cdp_ready"), g.web != nullptr);
        o.insert(QStringLiteral("api_hits"), g.apiCount);
        o.insert(QStringLiteral("time"), QDateTime::currentDateTime().toString(Qt::ISODate));
        httpJson(s, o);
        return;
    }

    if (route == QStringLiteral("/metrics")) {
        // CDP 的 Input.* 坐标是 CSS 像素，而 captureScreenshot 出的是物理像素；
        // 高 DPI 下两者差 dpr 倍，必须同时给出两套尺寸供调用方换算。
        callCdp(QStringLiteral("Page.getLayoutMetrics"), QJsonObject(),
                [s](bool ok, const QJsonObject& r) {
            const QJsonObject css  = r.value(QStringLiteral("cssLayoutViewport")).toObject();
            const QJsonObject phys = r.value(QStringLiteral("layoutViewport")).toObject();
            const double cw = css.value(QStringLiteral("clientWidth")).toDouble();
            const double ch = css.value(QStringLiteral("clientHeight")).toDouble();
            if (!ok || cw <= 0 || ch <= 0) {
                QJsonObject e; e.insert(QStringLiteral("ok"), false);
                e.insert(QStringLiteral("error"), QStringLiteral("getLayoutMetrics failed"));
                httpJson(s, e, 500); return;
            }
            QJsonObject o;
            o.insert(QStringLiteral("ok"), true);
            o.insert(QStringLiteral("css_width"), cw);
            o.insert(QStringLiteral("css_height"), ch);
            o.insert(QStringLiteral("phys_width"), phys.value(QStringLiteral("clientWidth")).toDouble());
            o.insert(QStringLiteral("phys_height"), phys.value(QStringLiteral("clientHeight")).toDouble());
            o.insert(QStringLiteral("dpr"), phys.value(QStringLiteral("clientWidth")).toDouble() / cw);
            httpJson(s, o);
        });
        return;
    }

    if (route == QStringLiteral("/screenshot")) {
        QJsonObject p;
        p.insert(QStringLiteral("format"), QStringLiteral("png"));
        p.insert(QStringLiteral("captureBeyondViewport"), false);
        callCdp(QStringLiteral("Page.captureScreenshot"), p,
                [s](bool ok, const QJsonObject& r) {
            const QByteArray png = QByteArray::fromBase64(
                r.value(QStringLiteral("data")).toString().toLatin1());
            if (!ok || png.isEmpty()) {
                QJsonObject e; e.insert(QStringLiteral("ok"), false);
                e.insert(QStringLiteral("error"), QStringLiteral("captureScreenshot failed"));
                httpJson(s, e, 500); return;
            }
            httpReply(s, 200, "image/png", png);
        });
        return;
    }

    if (route == QStringLiteral("/click")) {
        const double x = body.value(QStringLiteral("x")).toDouble();
        const double y = body.value(QStringLiteral("y")).toDouble();
        auto mouseEvt = [x, y](const QString& type, int buttons, int clickCount) {
            QJsonObject p;
            p.insert(QStringLiteral("type"), type);
            p.insert(QStringLiteral("x"), x);
            p.insert(QStringLiteral("y"), y);
            p.insert(QStringLiteral("button"), QStringLiteral("left"));
            p.insert(QStringLiteral("buttons"), buttons);
            p.insert(QStringLiteral("clickCount"), clickCount);
            return p;
        };
        logLine(QStringLiteral("CDP click (%1, %2)").arg(x, 0, 'f', 0).arg(y, 0, 'f', 0));
        callCdp(QStringLiteral("Input.dispatchMouseEvent"),
                mouseEvt(QStringLiteral("mouseMoved"), 0, 0),
                [s, mouseEvt](bool ok1, const QJsonObject&) {
            if (!ok1 || !s) { httpJson(s, cdpOk(false, QStringLiteral("click")), 500); return; }
            callCdp(QStringLiteral("Input.dispatchMouseEvent"),
                    mouseEvt(QStringLiteral("mousePressed"), 1, 1),
                    [s, mouseEvt](bool ok2, const QJsonObject&) {
                if (!ok2 || !s) { httpJson(s, cdpOk(false, QStringLiteral("click")), 500); return; }
                callCdp(QStringLiteral("Input.dispatchMouseEvent"),
                        mouseEvt(QStringLiteral("mouseReleased"), 0, 1),
                        [s](bool ok3, const QJsonObject&) {
                    httpJson(s, cdpOk(ok3, QStringLiteral("click")), ok3 ? 200 : 500);
                });
            });
        });
        return;
    }

    if (route == QStringLiteral("/text")) {
        const QString text = body.value(QStringLiteral("text")).toString();
        QJsonObject p; p.insert(QStringLiteral("text"), text);
        callCdp(QStringLiteral("Input.insertText"), p,
                [s](bool ok, const QJsonObject& r) {
            QJsonObject o = cdpOk(ok, QStringLiteral("insertText"));
            if (r.contains(QStringLiteral("error"))) o.insert(QStringLiteral("cdp_error"),
                                                              r.value(QStringLiteral("error")));
            httpJson(s, o, ok ? 200 : 500);
        });
        return;
    }

    if (route == QStringLiteral("/key")) {
        QString key = body.value(QStringLiteral("key")).toString(QStringLiteral("Enter"));
        int vk = 13, modifiers = 0;
        QString code = QStringLiteral("Enter");
        if (key == QStringLiteral("Ctrl+A")) {
            key = QStringLiteral("a"); code = QStringLiteral("KeyA"); vk = 65; modifiers = 2;
        } else if (key == QStringLiteral("Backspace")) { code = key; vk = 8; }
        else if (key == QStringLiteral("Delete"))    { code = key; vk = 46; }
        else if (key == QStringLiteral("Escape"))    { code = key; vk = 27; }
        else if (key == QStringLiteral("Tab"))       { code = key; vk = 9; }
        else if (key == QStringLiteral("ArrowDown")) { code = key; vk = 40; }
        auto keyEvt = [key, code, vk, modifiers](const QString& type) {
            QJsonObject p;
            p.insert(QStringLiteral("type"), type);
            p.insert(QStringLiteral("key"), key);
            p.insert(QStringLiteral("code"), code);
            p.insert(QStringLiteral("windowsVirtualKeyCode"), vk);
            p.insert(QStringLiteral("nativeVirtualKeyCode"), vk);
            p.insert(QStringLiteral("modifiers"), modifiers);
            // Enter 必须带 text：只发 keyDown+keyUp 页面不会触发表单提交
            // （实测公众号后台搜索框：无 text 时 URL 不变，带 text 时 query 生效）
            if (type == QStringLiteral("keyDown") && key == QStringLiteral("Enter")) {
                p.insert(QStringLiteral("text"), QStringLiteral("\r"));
                p.insert(QStringLiteral("unmodifiedText"), QStringLiteral("\r"));
            }
            return p;
        };
        callCdp(QStringLiteral("Input.dispatchKeyEvent"), keyEvt(QStringLiteral("keyDown")),
                [s, keyEvt](bool ok1, const QJsonObject&) {
            if (!ok1 || !s) { httpJson(s, cdpOk(false, QStringLiteral("key")), 500); return; }
            callCdp(QStringLiteral("Input.dispatchKeyEvent"), keyEvt(QStringLiteral("keyUp")),
                    [s](bool ok2, const QJsonObject&) {
                httpJson(s, cdpOk(ok2, QStringLiteral("key")), ok2 ? 200 : 500);
            });
        });
        return;
    }

    if (route == QStringLiteral("/cdp")) {
        const QString m = body.value(QStringLiteral("method")).toString();
        const QJsonObject p = body.value(QStringLiteral("params")).toObject();
        callCdp(m, p, [s, m](bool ok, const QJsonObject& r) {
            QJsonObject o = cdpOk(ok, m);
            o.insert(QStringLiteral("result"), r);
            httpJson(s, o, ok ? 200 : 500);
        });
        return;
    }

    QJsonObject e;
    e.insert(QStringLiteral("ok"), false);
    e.insert(QStringLiteral("error"), QStringLiteral("unknown route"));
    e.insert(QStringLiteral("path"), route);
    httpJson(s, e, 404);
}

static void startCdpHttp()
{
    auto* srv = new QTcpServer(qApp);
    QObject::connect(srv, &QTcpServer::newConnection, qApp, [srv]() {
        while (QTcpSocket* s = srv->nextPendingConnection()) {
            g_httpBuf.insert(s, QByteArray());
            QObject::connect(s, &QTcpSocket::readyRead, qApp, [s]() {
                QByteArray& buf = g_httpBuf[s];
                buf.append(s->readAll());
                const int headEnd = buf.indexOf("\r\n\r\n");
                if (headEnd < 0) return;
                const QByteArray head = buf.left(headEnd);
                int contentLength = 0;
                for (const QByteArray& line : head.split('\n')) {
                    if (line.toLower().startsWith("content-length:")) {
                        contentLength = line.mid(15).trimmed().toInt();
                        break;
                    }
                }
                if (buf.size() < headEnd + 4 + contentLength) return;  // body 未收齐
                const QByteArray body = buf.mid(headEnd + 4, contentLength);
                const QList<QByteArray> first = head.split('\n').value(0).trimmed().split(' ');
                const QString method = QString::fromLatin1(first.value(0));
                const QString path   = QString::fromLatin1(first.value(1));
                QJsonParseError pe{};
                const QJsonDocument d = QJsonDocument::fromJson(body, &pe);
                const QJsonObject obj = (pe.error == QJsonParseError::NoError && d.isObject())
                                      ? d.object() : QJsonObject();
                buf.clear();
                handleHttp(QPointer<QTcpSocket>(s), method, path, obj);
            });
            QObject::connect(s, &QTcpSocket::disconnected, qApp, [s]() {
                g_httpBuf.remove(s);
                s->deleteLater();
            });
        }
    });
    if (!srv->listen(QHostAddress::LocalHost, kCdpPort)) {
        logLine(QStringLiteral("CDP 控制端口监听失败：") + srv->errorString());
    } else {
        logLine(QStringLiteral("CDP 控制通道就绪 http://127.0.0.1:%1 （仅本机回环）")
                    .arg(kCdpPort));
    }
}

static void initWebView()
{
    QString dllPath = baseDir() + QStringLiteral("/WebView2Loader.dll");
    g.loader = LoadLibraryW(reinterpret_cast<LPCWSTR>(dllPath.utf16()));
    if (!g.loader) {
        logLine(QStringLiteral("WebView2Loader.dll 加载失败"));
        g.state = QStringLiteral("ERROR_NO_LOADER"); writeStatus();
        setBanner(QStringLiteral("缺少 WebView2Loader.dll"), false);
        return;
    }
    auto pfn = (CreateWebViewEnvFn)GetProcAddress(g.loader, "CreateCoreWebView2EnvironmentWithOptions");
    if (!pfn) {
        logLine(QStringLiteral("找不到 CreateCoreWebView2EnvironmentWithOptions"));
        g.state = QStringLiteral("ERROR_NO_ENTRY"); writeStatus();
        return;
    }
    QString profile = baseDir() + QStringLiteral("/MpProfile");
    QDir().mkpath(profile);
    logLine(QStringLiteral("会话目录 ") + profile);

    HRESULT hr = pfn(nullptr, reinterpret_cast<LPCWSTR>(profile.utf16()),
                     nullptr, new EnvReadyHandler);
    if (FAILED(hr)) {
        logLine(QStringLiteral("WebView2 环境创建失败 0x") + QString::number((ulong)hr, 16));
        g.state = QStringLiteral("ERROR_ENV"); writeStatus();
        setBanner(QStringLiteral("WebView2 运行时不可用"), false);
    }
}

HRESULT EnvReadyHandler::Invoke(HRESULT result, ICoreWebView2Environment* env)
{
    if (FAILED(result) || !env) return S_OK;
    env->AddRef();
    g.env = env;
    env->CreateCoreWebView2Controller(g.hwnd, new CtrlReadyHandler);
    return S_OK;
}

HRESULT CtrlReadyHandler::Invoke(HRESULT result, ICoreWebView2Controller* ctrl)
{
    if (FAILED(result) || !ctrl) return S_OK;
    ctrl->AddRef();
    g.ctrl = ctrl;
    if (FAILED(ctrl->get_CoreWebView2(&g.web)) || !g.web) return S_OK;

    ICoreWebView2Settings* settings = nullptr;
    if (SUCCEEDED(g.web->get_Settings(&settings)) && settings) {
        settings->put_IsWebMessageEnabled(TRUE);
        settings->Release();
    }
    g.web->add_NavigationCompleted(new NavCompletedHandler, &g.navTok);
    g.web->add_SourceChanged(new SourceChangedHandler, &g.srcTok);
    g.web->add_WebMessageReceived(new WebMessageHandler, &g.msgTok);

    QString js = QString::fromUtf8(kHookJs);
    g.web->AddScriptToExecuteOnDocumentCreated(
        reinterpret_cast<LPCWSTR>(js.utf16()), new ScriptInjectedHandler);

    g.state = QStringLiteral("WAITING_LOGIN");
    writeStatus();
    QMetaObject::invokeMethod(qApp, []() {
        logLine(QStringLiteral("WebView2 就绪，正在打开 mp.weixin.qq.com"));
        setBanner(QStringLiteral("正在打开公众号平台，请准备扫码"), false);
    }, Qt::QueuedConnection);
    g.web->Navigate(L"https://mp.weixin.qq.com/");
    return S_OK;
}

// ══════════════ 内置本地管理台 ══════════════════════════════════════
static void setAdminStatus(const QString& s)
{
    if (a.status) a.status->setText(s);
}

static bool adminPortOpen()
{
    QTcpSocket s;
    s.connectToHost(QHostAddress::LocalHost, kAdminPort);
    return s.waitForConnected(120);
}

static void startAdminProcess();
static void waitAdminReady();

static void adminNavigate()
{
    setAdminStatus(a.embedded && !a.proc
        ? QStringLiteral("管理台已就绪 · 内嵌 Python 单进程 · http://127.0.0.1:8766/ · 仅本机可访问")
        : QStringLiteral("管理台已就绪 · http://127.0.0.1:8766/ · 仅本机可访问"));
    if (a.web) a.web->Navigate(kAdminUrl);
}

// ── 在 Qt 进程内启动 CPython，跑 pipeline/_embedded_boot.py ──────────
// 用 LoadLibraryExW + GetProcAddress 而不是链接 python313.lib：
//   python313.dll 是 MSVC 编译的，MinGW 直接链 .lib 有 ABI/导入库风险；
//   Py_Initialize / PyRun_SimpleString / Py_FinalizeEx 都是纯 C 符号，
//   只传 const char*，跨 CRT 安全（实测探针通过）。
static bool startEmbeddedAdmin()
{
    const QString root = embedRoot();
    const QString dll  = root + QStringLiteral("/python313.dll");
    const QString boot = embedBoot();
    if (!QFile::exists(dll) || !QFile::exists(boot)) {
        logLine(QStringLiteral("[内嵌] 缺少 pyruntime/python313.dll 或 _embedded_boot.py，回退外部 Python"));
        return false;
    }
    QFile::remove(embedStopFile());     // 清掉上次异常退出残留的哨兵

    // 全部通过环境变量传给 Python，避免把 Windows 路径拼进源码字符串
    setWideEnv(L"PYTHONHOME", root);
    setWideEnv(L"PYTHONPATH", root + QStringLiteral("/python313.zip;")
                            + root + QStringLiteral("/DLLs;")
                            + baseDir() + QStringLiteral("/pipeline"));
    setWideEnv(L"MP_ADMIN_PORT", QString::number(kAdminPort));
    setWideEnv(L"MP_STOP_FILE",  embedStopFile());
    setWideEnv(L"MP_BOOT_FILE",  boot);
    setWideEnv(L"MP_EMBED_LOG",  embedLogFile());
    setWideEnv(L"MP_CDP_URL",    QStringLiteral("http://127.0.0.1:8765"));
    setWideEnv(L"PYTHONIOENCODING", QStringLiteral("utf-8"));

    const std::wstring wdll = dll.toStdWString();
    const QByteArray logPath = embedLogFile().toUtf8();
    g_embedDone.store(false);
    g_embedState.store(EMB_STARTING);

    g_embedThread.t = std::thread([wdll, logPath]() {
        auto wlog = [&logPath](const char* s) {
            FILE* f = fopen(logPath.constData(), "a");
            if (f) { fprintf(f, "%s\n", s); fclose(f); }
        };
        HMODULE h = LoadLibraryExW(wdll.c_str(), nullptr,
                                   LOAD_WITH_ALTERED_SEARCH_PATH);
        if (!h) {
            wlog("[内嵌] FAIL LoadLibraryExW python313.dll");
            g_embedState.store(EMB_FAILED);
            g_embedDone.store(true);
            return;
        }
        using FnInit = void(*)();
        using FnRun  = int(*)(const char*);
        using FnFin  = int(*)();
        auto pInit = reinterpret_cast<FnInit>(GetProcAddress(h, "Py_Initialize"));
        auto pRun  = reinterpret_cast<FnRun> (GetProcAddress(h, "PyRun_SimpleString"));
        auto pFin  = reinterpret_cast<FnFin> (GetProcAddress(h, "Py_FinalizeEx"));
        if (!pInit || !pRun || !pFin) {
            wlog("[内嵌] FAIL CPython 符号缺失");
            g_embedState.store(EMB_FAILED);
            g_embedDone.store(true);
            return;
        }
        // -mwindows 的 GUI 进程没有控制台，sys.stdout/stderr 可能是 None；
        // 必须先把它们落到文件，否则 Python 侧任何 print 都会抛异常。
        static const char* kBootstrap =
            "import sys, os, io\n"
            "_f = open(os.environ['MP_EMBED_LOG'], 'a', encoding='utf-8', buffering=1)\n"
            "sys.stdout = _f\n"
            "sys.stderr = _f\n"
            "print('[内嵌] CPython 已初始化', sys.version.split()[0], flush=True)\n"
            "import runpy\n"
            "runpy.run_path(os.environ['MP_BOOT_FILE'], run_name='__main__')\n";
        wlog("[内嵌] Py_Initialize ...");
        pInit();
        const int rc = pRun(kBootstrap);      // 内部 serve_forever()，阻塞到哨兵出现
        wlog(rc == 0 ? "[内嵌] bootstrap 正常返回" : "[内嵌] bootstrap 异常返回");
        if (pFin) pFin();
        wlog("[内嵌] Py_FinalizeEx 完成");
        g_embedState.store(rc == 0 ? EMB_STOPPED : EMB_FAILED);
        g_embedDone.store(true);
    });

    a.embedded = true;
    logLine(QStringLiteral("[内嵌] 已在本进程启动 CPython，等待 8766 就绪…"));
    setAdminStatus(QStringLiteral("管理台：内嵌 Python 启动中…"));
    a.waitTicks = 0;
    QTimer::singleShot(400, qApp, waitAdminReady);
    return true;
}

static void stopEmbeddedAdmin()
{
    const int st = g_embedState.load();
    if (st == EMB_IDLE || st == EMB_STOPPED || !g_embedThread.t.joinable()) return;

    QFile f(embedStopFile());
    if (f.open(QIODevice::WriteOnly)) { f.write("stop"); f.close(); }
    for (int i = 0; i < 25 && !g_embedDone.load(); ++i)
        QThread::msleep(80);              // 最多等 2 秒
    if (g_embedDone.load()) {
        g_embedThread.t.join();
        logLine(QStringLiteral("[内嵌] CPython 已优雅停止，端口已释放"));
    } else {
        g_embedThread.t.detach();
        logLine(QStringLiteral("[内嵌] 停止超时，交给进程退出回收"));
    }
    g_embedState.store(EMB_STOPPED);
}

static QStringList pythonCandidates()
{
    QStringList l;
    const QString fromEnv = QProcessEnvironment::systemEnvironment().value(
        QStringLiteral("MP_PYTHON"));
    if (!fromEnv.isEmpty()) l << fromEnv;
    l << QStringLiteral("python")
      << QStringLiteral("C:/Python312/python.exe")
      << QStringLiteral("C:/Python311/python.exe");
    return l;
}

static void startAdminProcess()
{
    if (a.pyCandidates.isEmpty()) a.pyCandidates = pythonCandidates();
    if (a.pyIndex >= a.pyCandidates.size()) {
        setAdminStatus(QStringLiteral("未找到可用的 Python，请手动运行 pipeline/start-ui.bat"));
        logLine(QStringLiteral("管理台：所有 Python 候选都启动失败"));
        return;
    }
    const QString py = a.pyCandidates.at(a.pyIndex);
    QProcess* p = new QProcess(qApp);
    a.proc = p;
    p->setProgram(py);
    p->setArguments({ QStringLiteral("pipeline.py"), QStringLiteral("ui"),
                      QStringLiteral("--port"), QString::number(kAdminPort) });
    p->setWorkingDirectory(baseDir() + QStringLiteral("/pipeline"));
    p->setProcessChannelMode(QProcess::MergedChannels);
    // 内嵌模式给本进程设过 PYTHONHOME/PYTHONPATH（指向精简运行时），
    // 回退到外部 Python 时必须剥离，否则外部解释器会被指错前缀而启动失败。
    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    env.remove(QStringLiteral("PYTHONHOME"));
    env.remove(QStringLiteral("PYTHONPATH"));
    env.remove(QStringLiteral("MP_BOOT_FILE"));
    env.remove(QStringLiteral("MP_STOP_FILE"));
    env.remove(QStringLiteral("MP_EMBED_LOG"));
    p->setProcessEnvironment(env);
    // 控制台黑框不弹出来（Qt 的 GUI 程序默认会给 console 子进程建窗口）
    p->setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments* args) {
        args->flags |= CREATE_NO_WINDOW;
        if (args->startupInfo) {
            args->startupInfo->dwFlags |= STARTF_USESHOWWINDOW;
            args->startupInfo->wShowWindow = SW_HIDE;
        }
    });
    QObject::connect(p, &QProcess::readyReadStandardOutput, qApp, [p]() {
        const QString s = QString::fromLocal8Bit(p->readAllStandardOutput()).trimmed();
        if (!s.isEmpty()) logLine(QStringLiteral("[管理台] ") + s.left(240));
    });
    QObject::connect(p, &QProcess::errorOccurred, qApp, [py](QProcess::ProcessError e) {
        logLine(QStringLiteral("[管理台] 进程异常 ") + py + QStringLiteral(" err=")
                + QString::number(int(e)));
    });
    QObject::connect(p, &QProcess::finished, qApp, [py](int code, QProcess::ExitStatus) {
        logLine(QStringLiteral("[管理台] 服务进程退出 ") + py
                + QStringLiteral(" code=") + QString::number(code));
    });
    p->start();
    logLine(QStringLiteral("[管理台] 正在用 ") + py
            + QStringLiteral(" 启动 pipeline.py ui --port 8766"));
    setAdminStatus(QStringLiteral("正在启动本地管理服务（") + py + QStringLiteral("）…"));
    a.waitTicks = 0;
    QTimer::singleShot(400, qApp, waitAdminReady);
}

static void waitAdminReady()
{
    if (adminPortOpen()) {
        if (a.embedded && !a.proc) {
            logLine(QStringLiteral("[内嵌] 8766 已就绪 · 单进程模式（无 python.exe 子进程）"));
        }
        adminNavigate();
        return;
    }
    // 内嵌线程异常退出 → 回收线程，只回退一次到 v4 的外部 Python
    if (g_embedState.load() == EMB_FAILED && !a.embedFallbackTried && !a.proc) {
        a.embedFallbackTried = true;
        if (g_embedThread.t.joinable()) g_embedThread.t.join();
        a.embedded = false;
        logLine(QStringLiteral("[内嵌] 启动失败，回退外部 Python（详见 embed.log）"));
        startAdminProcess();
        return;
    }
    // 当前候选秒退 → 换下一个 Python
    if (a.proc && a.proc->state() == QProcess::NotRunning && a.waitTicks >= 3) {
        a.proc->deleteLater();
        a.proc = nullptr;
        ++a.pyIndex;
        if (a.pyIndex < a.pyCandidates.size()) { startAdminProcess(); return; }
    }
    if (++a.waitTicks > 30) {
        setAdminStatus(QStringLiteral("服务启动超时，可手动运行 pipeline/start-ui.bat 后点「刷新」"));
        logLine(QStringLiteral("[管理台] 等待 8766 超时（内嵌=")
                + QString::number(g_embedState.load()) + QStringLiteral("）"));
        return;
    }
    QTimer::singleShot(400, qApp, waitAdminReady);
}

static void ensureAdminService()
{
    if (!a.web) return;
    if (adminPortOpen()) {
        logLine(QStringLiteral("[管理台] 检测到 8766 已有服务，直接复用（不重复启动）"));
        a.embedded = false;
        adminNavigate();
    } else if (!startEmbeddedAdmin()) {
        startAdminProcess();       // pyruntime 缺失 / DLL 加载失败时的兼容路径
    }
}

static void initAdminWebView()
{
    if (!g.loader) {
        setAdminStatus(QStringLiteral("WebView2Loader.dll 未加载，管理台不可用"));
        return;
    }
    auto pfn = (CreateWebViewEnvFn)GetProcAddress(
        g.loader, "CreateCoreWebView2EnvironmentWithOptions");
    if (!pfn) {
        setAdminStatus(QStringLiteral("WebView2 入口函数缺失，管理台不可用"));
        return;
    }
    // 独立 profile：管理台是 http://127.0.0.1，不共用公众号的登录 cookie
    const QString profile = baseDir() + QStringLiteral("/MpAdminProfile");
    QDir().mkpath(profile);
    const HRESULT hr = pfn(nullptr, reinterpret_cast<LPCWSTR>(profile.utf16()),
                           nullptr, new AdminEnvReadyHandler);
    if (FAILED(hr)) {
        logLine(QStringLiteral("[管理台] WebView2 环境创建失败 0x")
                + QString::number((ulong)hr, 16));
        setAdminStatus(QStringLiteral("WebView2 环境不可用"));
    }
}

HRESULT AdminEnvReadyHandler::Invoke(HRESULT result, ICoreWebView2Environment* env)
{
    if (FAILED(result) || !env) {
        logLine(QStringLiteral("[管理台] WebView2 环境回调失败 0x")
                + QString::number((ulong)result, 16));
        return S_OK;
    }
    env->AddRef();
    a.env = env;
    env->CreateCoreWebView2Controller(a.hwnd, new AdminCtrlReadyHandler);
    return S_OK;
}

HRESULT AdminCtrlReadyHandler::Invoke(HRESULT result, ICoreWebView2Controller* ctrl)
{
    if (FAILED(result) || !ctrl) {
        logLine(QStringLiteral("[管理台] WebView2 控制器创建失败 0x")
                + QString::number((ulong)result, 16));
        return S_OK;
    }
    ctrl->AddRef();
    a.ctrl = ctrl;
    if (FAILED(ctrl->get_CoreWebView2(&a.web)) || !a.web) return S_OK;
    // 管理台页面不注入任何 hook，也不写 nav_history / events.log
    ICoreWebView2Settings* st = nullptr;
    if (SUCCEEDED(a.web->get_Settings(&st)) && st) {
        st->put_IsWebMessageEnabled(FALSE);
        st->Release();
    }
    a.ready = true;
    QMetaObject::invokeMethod(qApp, []() {
        logLine(QStringLiteral("[管理台] 第二个 WebView2 就绪（独立 profile）"));
        ensureAdminService();
    }, Qt::QueuedConnection);
    return S_OK;
}

static void applyAdminSizes()
{
    if (!a.split || !a.pane || !a.pane->isVisible()) return;
    const int total = qMax(900, a.split->width());
    // 管理台是三栏布局，默认多给一点宽度，避免在窄分屏里出现横向滚动条
    const int adminW = int(total * (a.wide ? 0.78 : 0.56));
    a.split->setSizes({ total - adminW, adminW });
}

static void toggleAdminPane()
{
    if (!a.pane) return;
    const bool show = a.pane->isHidden();
    a.pane->setVisible(show);          // 只隐藏右侧；左侧公众号 WebView2 始终可见
    if (show) QTimer::singleShot(80, qApp, applyAdminSizes);
}

static QWidget* buildAdminPane()
{
    QWidget* pane = new QWidget;
    auto* v = new QVBoxLayout(pane);
    v->setContentsMargins(0, 0, 0, 0);
    v->setSpacing(4);

    a.status = new QLabel(QStringLiteral("正在启动本地管理台…"));
    a.status->setStyleSheet(QStringLiteral(
        "background:#E8F5E9;color:#1B5E20;padding:6px;font-size:12px;"));
    v->addWidget(a.status);

    auto* bar = new QHBoxLayout;
    auto* bHome   = new QPushButton(QStringLiteral("管理台首页"));
    auto* bReload = new QPushButton(QStringLiteral("刷新"));
    auto* bWide   = new QPushButton(QStringLiteral("加宽 / 还原"));
    auto* bExt    = new QPushButton(QStringLiteral("系统浏览器打开"));
    auto* bHide   = new QPushButton(QStringLiteral("收起"));
    bar->addWidget(bHome); bar->addWidget(bReload); bar->addWidget(bWide);
    bar->addStretch(1); bar->addWidget(bExt); bar->addWidget(bHide);
    v->addLayout(bar);

    a.host = new QWidget;
    a.host->setMinimumSize(360, 240);
    a.host->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
    a.host->setStyleSheet(QStringLiteral("background:#FFFFFF;border:1px solid #BBB;"));
    v->addWidget(a.host, 1);
    a.hwnd = reinterpret_cast<HWND>(a.host->winId());

    QObject::connect(bHome,   &QPushButton::clicked, pane, []() {
        if (a.web) a.web->Navigate(kAdminUrl);
    });
    QObject::connect(bReload, &QPushButton::clicked, pane, []() {
        if (a.web) a.web->Reload();
    });
    QObject::connect(bWide,   &QPushButton::clicked, pane, []() {
        a.wide = !a.wide; applyAdminSizes();
    });
    QObject::connect(bExt,    &QPushButton::clicked, pane, []() {
        QDesktopServices::openUrl(QUrl(QStringLiteral("http://127.0.0.1:8766/")));
    });
    QObject::connect(bHide,   &QPushButton::clicked, pane, []() {
        if (a.pane) a.pane->hide();
    });
    return pane;
}

int main(int argc, char** argv)
{
    QApplication app(argc, argv);
    QWidget window;
    window.setWindowTitle(QStringLiteral(
        "公众号工作台 v5  |  左侧：公众号后台 / CDP 8765   右侧：本地素材库 · 流水线 8766（内嵌 Python 单进程）"));
    // 按屏幕可用区域自适应：窗口比屏幕高时底部按钮会被挤出屏幕，看不见也点不到
    const QRect avail = QApplication::primaryScreen()->availableGeometry();
    window.resize(qMin(1180, int(avail.width()  * 0.92)),
                  qMin(860,  int(avail.height() * 0.92)));
    window.move(avail.center() - window.rect().center());

    auto* root = new QVBoxLayout(&window);
    auto* head = new QLabel(QStringLiteral(
        "左侧公众号后台只做只读旁观 + CDP 注入；右侧管理台只生成/保存草稿，不群发、不发表。"
        "两个 WebView2 互不干扰；管理台由本进程内嵌的 CPython 提供，无需另装 Python。"));
    head->setStyleSheet(QStringLiteral("color:#555;padding:4px;"));
    root->addWidget(head);

    g.status = new QLabel(QStringLiteral("正在启动 WebView2…"));
    g.status->setStyleSheet(QStringLiteral("background:#FFF3E0;color:#8A4B00;padding:10px;font-size:15px;font-weight:bold;"));
    root->addWidget(g.status);

    // 左右并排两个 WebView2：左侧必须常驻可见，8765 的 CDP 抓屏 / 注入才不会失效
    a.split = new QSplitter(Qt::Horizontal);
    a.split->setHandleWidth(7);
    a.split->setChildrenCollapsible(false);

    g.host = new QWidget;
    g.host->setMinimumSize(320, 240);   // 别用大 minSize，会把窗口撑出屏幕
    g.host->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
    g.host->setStyleSheet(QStringLiteral("background:#FFFFFF;border:1px solid #CCC;"));
    a.split->addWidget(g.host);
    g.hwnd = reinterpret_cast<HWND>(g.host->winId());

    a.pane = buildAdminPane();
    a.split->addWidget(a.pane);
    root->addWidget(a.split, 1);

    g.log = new QPlainTextEdit;
    g.log->setReadOnly(true);
    g.log->setMaximumBlockCount(500);
    g.log->setFixedHeight(110);
    g.log->setStyleSheet(QStringLiteral("font-family:Consolas;font-size:12px;background:#FAFAFA;"));
    root->addWidget(g.log);

    auto* row = new QHBoxLayout;
    auto* btnExport = new QPushButton(QStringLiteral("导出会话快照"));
    auto* btnReload = new QPushButton(QStringLiteral("重新打开登录页"));
    auto* btnCheck  = new QPushButton(QStringLiteral("刷新状态文件"));
    auto* btnAdmin  = new QPushButton(QStringLiteral("⛶ 本地管理"));
    auto* btnQuit   = new QPushButton(QStringLiteral("退出"));
    btnAdmin->setStyleSheet(QStringLiteral("color:#1B5E20;font-weight:bold;"));
    row->addWidget(btnExport); row->addWidget(btnReload); row->addWidget(btnCheck);
    row->addWidget(btnAdmin);
    row->addStretch(1); row->addWidget(btnQuit);
    root->addLayout(row);

    QObject::connect(btnExport, &QPushButton::clicked, &window, []() { exportSession(); });
    QObject::connect(btnReload, &QPushButton::clicked, &window, []() {
        if (g.web) { logLine(QStringLiteral("手动重新导航")); g.web->Navigate(L"https://mp.weixin.qq.com/"); }
    });
    QObject::connect(btnCheck, &QPushButton::clicked, &window, []() {
        writeStatus();
        logLine(QStringLiteral("状态已写入 status.txt  当前=") + g.state);
    });
    QObject::connect(btnAdmin, &QPushButton::clicked, &window, []() { toggleAdminPane(); });
    QObject::connect(btnQuit, &QPushButton::clicked, &window, &QWidget::close);

    auto* timer = new QTimer(&window);
    QObject::connect(timer, &QTimer::timeout, &window, []() {
        if (g.ctrl && g.host) {
            const qreal dpr = g.host->devicePixelRatioF();
            RECT r = { 0, 0, (LONG)(g.host->width() * dpr), (LONG)(g.host->height() * dpr) };
            g.ctrl->put_Bounds(r);
        }
        if (a.ctrl && a.host) {
            const bool vis = a.host->isVisible();
            if (vis) {
                const qreal dpr2 = a.host->devicePixelRatioF();
                RECT r2 = { 0, 0, (LONG)(a.host->width() * dpr2),
                                   (LONG)(a.host->height() * dpr2) };
                a.ctrl->put_Bounds(r2);
            }
            a.ctrl->put_IsVisible(vis ? TRUE : FALSE);
        }
    });
    timer->start(400);

    QObject::connect(&app, &QApplication::aboutToQuit, []() {
        // v5：先让内嵌 CPython 优雅收尾（哨兵 → srv.shutdown → Py_FinalizeEx → join），
        // 再关 WebView2；否则 daemon 线程会在 COM 释放后继续轮询 8765。
        stopEmbeddedAdmin();
        if (g.web) {
            g.web->remove_NavigationCompleted(g.navTok);
            g.web->remove_SourceChanged(g.srcTok);
            g.web->remove_WebMessageReceived(g.msgTok);
        }
        if (g.ctrl) { g.ctrl->Close(); g.ctrl->Release(); }
        if (g.web) g.web->Release();
        if (g.env) g.env->Release();
        // 管理台：内嵌模式已在上面优雅停止；这里只回收「仅由本程序拉起」的外部 Python
        if (a.ctrl) { a.ctrl->Close(); a.ctrl->Release(); }
        if (a.web) a.web->Release();
        if (a.env) a.env->Release();
        if (a.proc) {
            a.proc->kill();
            a.proc->waitForFinished(600);   // 外部已有服务时 a.proc 为空，绝不误杀
        }
        if (g.loader) FreeLibrary(g.loader);
        g.state = QStringLiteral("EXITED");
        writeStatus();
    });

    writeStatus();
    logLine(QStringLiteral("=== MpLoginProbe v5 启动（CDP 8765 + 内嵌 Python 管理台 8766） ==="));
    startCdpHttp();
    window.showMaximized();   // 贴满可用区域：底部按钮行始终在屏幕内，便于人工/视觉点击
    QTimer::singleShot(200, &window, applyAdminSizes);
    QTimer::singleShot(300, &window, initWebView);
    QTimer::singleShot(900, &window, initAdminWebView);   // 等 g.loader 加载完成
    return app.exec();
}
