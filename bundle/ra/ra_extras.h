#pragma once

namespace gbarecomp { class HostOverlay; }

// Installed as RunOptions::host_overlay by the generated host when the
// build carries RetroAchievements: lazily initialises the RA client
// on the first presented frame, pumps rc_client once per frame, and
// draws login / achievement toasts through the runtime's host
// overlay (the framework's chrome layer — it never touches guest
// memory or the guest framebuffer).
void gba_ra_overlay(gbarecomp::HostOverlay* overlay);
