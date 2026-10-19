#pragma once

class Display;

namespace memoria {

// Full-screen network-error page shown while activation keeps retrying. The
// caller owns the retry policy and the QR/bootstrap lifecycle; these functions
// only draw the page and serialise LVGL with the display lock.
bool ShowActivationNetworkErrorPage(Display* display, const char* detail);
void HideActivationNetworkErrorPage(Display* display);

}  // namespace memoria
