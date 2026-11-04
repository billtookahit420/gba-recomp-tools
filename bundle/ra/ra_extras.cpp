/* ra_extras.cpp — RetroAchievements for gbarecomp games.
 *
 * Plugs into the game through RunOptions::host_overlay, the
 * framework's per-present host-chrome callback (runtime/
 * host_overlay.h): the generated host installs gba_ra_overlay there
 * when the build carries RA. All RA work happens lazily on the first
 * overlay call, on the present thread: no races, and rom.cfg already
 * names the ROM the launcher picked.
 *
 * Ported from the GB tool's RA module (same config format, same
 * server plumbing, same callback-polarity fix); the GBA differences:
 *
 *  - Identity: the ROM file the launcher picked (rom.cfg next to the
 *    exe, written by the launcher's asset picker; falling back to a
 *    .gba beside the exe or under roms/), hashed with rcheevos' GBA
 *    algorithm — the whole-file MD5 (rc_hash_generate_from_file;
 *    RC_CONSOLE_GAMEBOY_ADVANCE = 5 in the vendored rc_consoles.h).
 *  - Memory: rcheevos' GBA map is a compacted space (IWRAM at
 *    0x000000, EWRAM at 0x008000, save RAM at 0x048000 — see
 *    rc_consoleinfo.c); each address is translated back to the real
 *    bus address and read through the runtime's bus_read_u8()
 *    (src/armv4t/runtime_arm.h), which models every region including
 *    the save window the way the guest sees it.
 *  - Pop-ups: the host overlay IS a toast channel (unlike the GB
 *    runtime, which had none): fill_rect + text in View space,
 *    drawn after the game image on every present while a toast is
 *    live. Same 4-second lifetime and the same RA_DISABLE_POPUPS
 *    behaviour as the family (hiding pop-ups hides the login toast
 *    too: one channel).
 *  - Login: retroachievements.toml next to the exe (username +
 *    Connect api_token). Absent file = RA off, game runs normally.
 *
 * Hardcore note: rc_client is told the toml's hardcore flag, but
 * RetroAchievements counts hardcore only from approved clients; a
 * recomp client is an unknown emulator, so unlocks register as
 * Casual/softcore. Never spoof another client's identity.
 */
#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "rc_client.h"
#include "rc_consoles.h"
#include "rc_hash.h"
#include "ra_http.h"

#include "runtime/host_overlay.h"
#include "armv4t/runtime_arm.h"

#include "ra_extras.h"

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <unistd.h>
#include <limits.h>
#endif

namespace {

/* ------------------------------------------------------------------ */
/* Config: retroachievements.toml next to the exe                      */
/* ------------------------------------------------------------------ */

struct RaConfig {
    std::string username;
    std::string api_token;
    bool hardcore = false;
    bool present = false;
};

std::string ra_exe_dir() {
#ifdef _WIN32
    char path[MAX_PATH];
    DWORD n = GetModuleFileNameA(NULL, path, (DWORD)sizeof(path));
    if (n == 0 || n >= sizeof(path)) return std::string();
    std::string s(path);
    size_t slash = s.find_last_of("\\/");
    return (slash == std::string::npos) ? std::string() : s.substr(0, slash);
#else
    char path[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", path, sizeof(path) - 1);
    if (n <= 0) return std::string();
    path[n] = '\0';
    std::string s(path);
    size_t slash = s.find_last_of('/');
    return (slash == std::string::npos) ? std::string() : s.substr(0, slash);
#endif
}

std::string ra_trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t' || s[a] == '\r' || s[a] == '\n')) a++;
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t' || s[b - 1] == '\r' || s[b - 1] == '\n')) b--;
    return s.substr(a, b - a);
}

std::string ra_unquote(const std::string& s) {
    if (s.size() >= 2 && s.front() == '"' && s.back() == '"')
        return s.substr(1, s.size() - 2);
    if (s.size() >= 2 && s.front() == '\'' && s.back() == '\'')
        return s.substr(1, s.size() - 2);
    return s;
}

RaConfig ra_read_config(const std::string& exe_dir) {
    RaConfig cfg;
    std::string path = exe_dir + "/retroachievements.toml";
    FILE* f = fopen(path.c_str(), "r");
    char line[1024];
    if (!f) return cfg;
    cfg.present = true;
    while (fgets(line, sizeof(line), f)) {
        std::string s = ra_trim(line);
        size_t eq;
        std::string key, val;
        if (s.empty() || s[0] == '#') continue;
        eq = s.find('=');
        if (eq == std::string::npos) continue;
        key = ra_trim(s.substr(0, eq));
        val = ra_unquote(ra_trim(s.substr(eq + 1)));
        if (key == "username") cfg.username = val;
        else if (key == "api_token") cfg.api_token = val;
        else if (key == "hardcore") cfg.hardcore = (val == "true" || val == "1");
    }
    fclose(f);
    return cfg;
}

/* ------------------------------------------------------------------ */
/* ROM path: the runtime's own resolution order — rom.cfg next to the  */
/* exe (first line = path), else a .gba beside the exe, else one       */
/* under roms/ (mirrors the framework asset picker).                   */
/* ------------------------------------------------------------------ */

bool ra_file_exists(const std::string& p) {
    FILE* f = fopen(p.c_str(), "rb");
    if (f) { fclose(f); return true; }
    return false;
}

std::string ra_scan_rom_dir(const std::string& dir) {
    std::error_code ec;
    std::vector<std::string> hits;
    for (const auto& e : std::filesystem::directory_iterator(dir, ec)) {
        if (ec) break;
        if (!e.is_regular_file(ec)) continue;
        std::string name = e.path().filename().string();
        std::string low = name;
        for (auto& c : low) c = (char)tolower((unsigned char)c);
        if (low.size() > 4 && low.compare(low.size() - 4, 4, ".gba") == 0)
            hits.push_back(e.path().string());
    }
    std::sort(hits.begin(), hits.end());
    return hits.empty() ? std::string() : hits.front();
}

std::string ra_rom_path(const std::string& exe_dir) {
    std::string cfg = exe_dir + "/rom.cfg";
    FILE* f = fopen(cfg.c_str(), "r");
    if (f) {
        char line[4096];
        std::string p;
        if (fgets(line, sizeof(line), f)) p = ra_trim(line);
        fclose(f);
        if (!p.empty()) {
            if (ra_file_exists(p)) return p;
            std::string joined = exe_dir + "/" + p;
            if (ra_file_exists(joined)) return joined;
        }
    }
    std::string found = ra_scan_rom_dir(exe_dir);
    if (!found.empty()) return found;
    return ra_scan_rom_dir(exe_dir + "/roms");
}

/* ------------------------------------------------------------------ */
/* Memory peek: rcheevos' compacted GBA map translated back to real    */
/* bus addresses, read via the runtime's bus accessor.                 */
/*   rc 0x000000-0x007FFF -> bus 0x03000000 (32KB IWRAM)                */
/*   rc 0x008000-0x047FFF -> bus 0x02000000 (256KB EWRAM)               */
/*   rc 0x048000-0x057FFF -> bus 0x0E000000 (64KB save window)          */
/* ------------------------------------------------------------------ */

uint32_t ra_bus_addr(uint32_t a) {
    if (a < 0x8000u) return 0x03000000u + a;
    if (a < 0x48000u) return 0x02000000u + (a - 0x8000u);
    if (a < 0x58000u) return 0x0E000000u + (a - 0x48000u);
    return 0xFFFFFFFFu;
}

uint32_t ra_read_memory(uint32_t address, uint8_t* buffer, uint32_t num_bytes,
                        rc_client_t* /*client*/) {
    if (!buffer || num_bytes == 0) return 0;
    uint32_t done = 0;
    while (done < num_bytes) {
        uint32_t bus = ra_bus_addr(address + done);
        buffer[done++] = (bus == 0xFFFFFFFFu) ? 0 : bus_read_u8(bus);
    }
    return done;
}

/* ------------------------------------------------------------------ */
/* Server calls: detached thread + game-thread delivery                */
/* ------------------------------------------------------------------ */

struct RaPending {
    rc_client_server_callback_t callback = nullptr;
    void* callback_data = nullptr;
    char* body = nullptr;
    size_t body_len = 0;
    int http_status = 0;
};

std::mutex g_pending_mutex;
std::vector<RaPending> g_pending;

void ra_server_call(const rc_api_request_t* request,
                    rc_client_server_callback_t callback, void* callback_data,
                    rc_client_t* /*client*/) {
    std::string url = request->url ? request->url : "";
    std::string post = request->post_data ? request->post_data : "";
    std::string ctype = request->content_type ? request->content_type : "";
    std::thread([url, post, ctype, callback, callback_data]() {
        char* body = nullptr;
        size_t body_len = 0;
        int status = 0;
        RaPending p;
        if (ra_http_post(url.c_str(), post.c_str(), ctype.c_str(),
                         &body, &body_len, &status) != 0) {
            body = nullptr;
            body_len = 0;
            status = 0;
        }
        p.callback = callback;
        p.callback_data = callback_data;
        p.body = body;
        p.body_len = body_len;
        p.http_status = status;
        {
            std::lock_guard<std::mutex> lock(g_pending_mutex);
            g_pending.push_back(p);
        }
    }).detach();
}

void ra_deliver_pending() {
    std::vector<RaPending> due;
    {
        std::lock_guard<std::mutex> lock(g_pending_mutex);
        due.swap(g_pending);
    }
    for (auto& p : due) {
        rc_api_server_response_t resp;
        memset(&resp, 0, sizeof(resp));
        resp.body = p.body ? p.body : "";
        resp.body_length = p.body_len;
        resp.http_status_code = p.http_status;
        if (p.callback) p.callback(&resp, p.callback_data);
        free(p.body);
    }
}

/* ------------------------------------------------------------------ */
/* Toasts: drawn through the host overlay (View space) on every        */
/* present while live. The overlay is host chrome: it never touches    */
/* the guest framebuffer, so savestates/frame hashes are unaffected.   */
/* ------------------------------------------------------------------ */

struct RaToast {
    std::string title;
    std::string body;
    std::chrono::steady_clock::time_point until{};
    bool active = false;
};
RaToast g_toast;

void ra_toast_show(const std::string& title, const std::string& body) {
#ifdef RA_DISABLE_POPUPS
    /* Recomp Tools option: pop-ups hidden at build time. This also
       hides the login toast, which uses the same channel. */
    (void)title; (void)body;
    return;
#else
    g_toast.title = title;
    g_toast.body = body;
    g_toast.until = std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(4000);
    g_toast.active = true;
#endif
}

void ra_toast_draw(gbarecomp::HostOverlay* ov) {
    if (!g_toast.active) return;
    if (std::chrono::steady_clock::now() >= g_toast.until) {
        g_toast.active = false;
        return;
    }
    using namespace gbarecomp;
    const float vw = (float)ov->view_width();
    const float vh = (float)ov->view_height();
    if (vw <= 0 || vh <= 0) return;
    const float size = 9.0f;
    float tw = 0.0f;
    if (ov->text_supported()) {
        tw = std::max(ov->text_width(g_toast.title.c_str(), size,
                                     OverlaySpace::View),
                      ov->text_width(g_toast.body.c_str(), size,
                                     OverlaySpace::View));
    }
    const float box_w = std::min(vw - 8.0f, tw + 14.0f);
    const float box_h = 30.0f;
    const float box_x = (vw - box_w) * 0.5f;
    const float box_y = 6.0f;
    ov->fill_rect(box_x, box_y, box_w, box_h, 4.0f,
                  OverlayColor{14, 14, 18, 228}, OverlaySpace::View);
    ov->stroke_rect(box_x, box_y, box_w, box_h, 4.0f, 1.0f,
                    OverlayColor{255, 205, 80, 255}, OverlaySpace::View);
    if (ov->text_supported()) {
        ov->text(box_x + 7, box_y + 3, size,
                 OverlayColor{255, 205, 80, 255}, g_toast.title.c_str(),
                 OverlayAlign::Left, OverlaySpace::View);
        ov->text(box_x + 7, box_y + 3 + size + 4, size,
                 OverlayColor{240, 240, 240, 255}, g_toast.body.c_str(),
                 OverlayAlign::Left, OverlaySpace::View);
    }
}

/* ------------------------------------------------------------------ */
/* Events + async callbacks                                            */
/* ------------------------------------------------------------------ */

void ra_event_handler(const rc_client_event_t* event, rc_client_t* /*client*/) {
    if (!event) return;
    switch (event->type) {
        case RC_CLIENT_EVENT_ACHIEVEMENT_TRIGGERED:
            if (event->achievement) {
                fprintf(stdout,
                        "[RA] Achievement unlocked: %s (%u points) - %s\n",
                        event->achievement->title ? event->achievement->title : "?",
                        event->achievement->points,
                        event->achievement->description
                            ? event->achievement->description
                            : "");
                fflush(stdout);
                ra_toast_show(
                    event->achievement->title ? event->achievement->title : "?",
                    event->achievement->description
                        ? event->achievement->description
                        : "");
            }
            break;
        case RC_CLIENT_EVENT_GAME_COMPLETED:
            fprintf(stdout, "[RA] All achievements for this game earned!\n");
            fflush(stdout);
            break;
        case RC_CLIENT_EVENT_SERVER_ERROR:
            if (event->server_error) {
                fprintf(stderr, "[RA] Server error (%s): %s\n",
                        event->server_error->api ? event->server_error->api : "?",
                        event->server_error->error_message
                            ? event->server_error->error_message
                            : "?");
            }
            break;
        case RC_CLIENT_EVENT_DISCONNECTED:
            fprintf(stderr, "[RA] Disconnected: unlocks will queue until reconnect.\n");
            break;
        case RC_CLIENT_EVENT_RECONNECTED:
            fprintf(stdout, "[RA] Reconnected: queued unlocks submitted.\n");
            fflush(stdout);
            break;
        default:
            break;
    }
}

void ra_login_callback(int result, const char* error_message,
                       rc_client_t* /*client*/, void* /*userdata*/) {
    /* rcheevos reports RC_OK (0) on success, nonzero on failure. */
    if (result == 0) {
        fprintf(stdout, "[RA] Logged in.\n");
        ra_toast_show("RetroAchievements", "Logged in");
    } else {
        fprintf(stdout, "[RA] Login failed: %s\n",
                error_message ? error_message : "unknown error");
        ra_toast_show("RetroAchievements",
                      "Login failed - check retroachievements.toml");
    }
    fflush(stdout);
}

void ra_load_callback(int result, const char* error_message,
                      rc_client_t* /*client*/, void* /*userdata*/) {
    if (result == 0) {
        fprintf(stdout, "[RA] Game achievements loaded.\n");
    } else {
        fprintf(stderr, "[RA] Could not load game achievements: %s\n",
                error_message ? error_message : "unknown error");
    }
    fflush(stdout);
}

/* ------------------------------------------------------------------ */
/* Init (lazy, first presented frame) + the overlay entry point        */
/* ------------------------------------------------------------------ */

rc_client_t* g_client = nullptr;
bool g_init_done = false;

void ra_init() {
    std::string exe_dir = ra_exe_dir();
    /* Dev-only test hook (NOT shipped in packages): if ra_test_toast.txt
     * sits next to the exe, pop its contents as a toast so the pop-up
     * channel can be verified without a real RA unlock. */
    {
        std::string test_path = exe_dir + "/ra_test_toast.txt";
        FILE* tf = fopen(test_path.c_str(), "r");
        if (tf) {
            char tline[256] = "";
            char bline[512] = "";
            if (fgets(tline, sizeof(tline), tf)) {
                std::string t = ra_trim(tline);
                std::string b;
                if (fgets(bline, sizeof(bline), tf)) b = ra_trim(bline);
                if (t.empty()) t = "Test Achievement";
                if (b.empty()) b = "In-game pop-ups are working!";
                ra_toast_show(t, b);
                fprintf(stdout, "[RA] test toast armed: %s\n", t.c_str());
                fflush(stdout);
            }
            fclose(tf);
        }
    }
    RaConfig cfg = ra_read_config(exe_dir);
    if (!cfg.present) {
        fprintf(stdout,
                "[RA] Disabled: no retroachievements.toml next to the exe.\n"
                "[RA] To enable: create retroachievements.toml with your\n"
                "[RA] username and RetroAchievements API token, then restart.\n");
        fflush(stdout);
        return;
    }
    if (cfg.username.empty() || cfg.api_token.empty()) {
        fprintf(stderr,
                "[RA] Disabled: retroachievements.toml is missing username "
                "or api_token.\n");
        return;
    }
    std::string rom = ra_rom_path(exe_dir);
    if (rom.empty()) {
        fprintf(stderr, "[RA] Disabled: could not find the ROM "
                        "(no rom.cfg, no .gba beside the exe).\n");
        return;
    }
    char hash[33];
    memset(hash, 0, sizeof(hash));
    if (!rc_hash_generate_from_file(hash, RC_CONSOLE_GAMEBOY_ADVANCE,
                                    rom.c_str())) {
        fprintf(stderr, "[RA] Disabled: could not hash ROM %s\n", rom.c_str());
        return;
    }
    fprintf(stdout, "[RA] ROM hash: %s\n", hash);

    g_client = rc_client_create(ra_read_memory, ra_server_call);
    if (!g_client) {
        fprintf(stderr, "[RA] Disabled: rc_client_create failed.\n");
        return;
    }
    rc_client_set_event_handler(g_client, ra_event_handler);
    rc_client_set_hardcore_enabled(g_client, cfg.hardcore ? 1 : 0);
    fprintf(stdout, "[RA] Logging in as %s (hardcore %s)...\n",
            cfg.username.c_str(), cfg.hardcore ? "on" : "off");
    fflush(stdout);
    rc_client_begin_login_with_token(g_client, cfg.username.c_str(),
                                     cfg.api_token.c_str(), ra_login_callback,
                                     nullptr);
    rc_client_begin_load_game(g_client, hash, ra_load_callback, nullptr);
}

} /* namespace */

/* The framework's per-present host-overlay callback, installed by
 * the generated host (RunOptions::host_overlay) when the build
 * carries RetroAchievements. */
void gba_ra_overlay(gbarecomp::HostOverlay* overlay) {
    if (!overlay) return;
    if (!g_init_done) {
        g_init_done = true;
        ra_init();
    }
    if (g_client) ra_deliver_pending();
    if (g_client) rc_client_do_frame(g_client);
    ra_toast_draw(overlay);
}
