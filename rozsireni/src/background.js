/* Aladin — background script.
 *
 * Opens the panel the way each browser can:
 *   Chrome, Edge           side panel (chrome.sidePanel)
 *   Firefox                sidebar (browser.sidebarAction)
 *   Opera                  sidebar from the manifest; the toolbar button opens
 *                          a small window as a fallback
 *   Safari                 popup (manifest default_popup, nothing to do here)
 *
 * The right-click menu "Zeptat se Aladina" stores what was clicked (selection or
 * page) for the panel and opens it. */
const B = globalThis.browser ?? globalThis.chrome;

const sidePanel = globalThis.chrome && chrome.sidePanel;
const sidebar = globalThis.browser && browser.sidebarAction;

if (sidePanel && sidePanel.setPanelBehavior) {
  sidePanel.setPanelBehavior({openPanelOnActionClick: true}).catch(() => {});
}

async function otevritOkno(tab) {
  // A small window next to the page, for browsers without a side panel API.
  const url = B.runtime.getURL('panel.html') + '?okno=1' + (tab && tab.windowId ? '&w=' + tab.windowId : '');
  await B.windows.create({url, type: 'popup', width: 440, height: 760});
}

async function otevrit(tab) {
  if (sidePanel && sidePanel.open && tab) {
    try { await sidePanel.open({windowId: tab.windowId}); return; } catch (_) { /* fallback below */ }
  }
  if (sidebar && sidebar.open) {
    try { await sidebar.open(); return; } catch (_) { /* fallback below */ }
  }
  await otevritOkno(tab);
}

// Chrome/Edge open the side panel by themselves (setPanelBehavior). Safari has
// a popup. Firefox toggles the sidebar; Opera (and anything else) gets a window.
if (!(sidePanel && sidePanel.setPanelBehavior) && B.action && B.action.onClicked) {
  B.action.onClicked.addListener((tab) => {
    if (sidebar && sidebar.toggle) { sidebar.toggle(); return; }
    otevritOkno(tab);
  });
}

function menu() {
  if (!B.contextMenus) return;
  B.contextMenus.removeAll(() => {
    B.contextMenus.create({id: 'aladin-vyber', title: 'Zeptat se Aladina na „%s“', contexts: ['selection']});
    B.contextMenus.create({id: 'aladin-stranka', title: 'Zeptat se Aladina na tuhle stránku', contexts: ['page']});
  });
}
B.runtime.onInstalled.addListener(menu);
if (B.runtime.onStartup) B.runtime.onStartup.addListener(menu);

if (B.contextMenus) {
  B.contextMenus.onClicked.addListener((info, tab) => {
    // Opening the panel must happen right inside the click (user gesture).
    const ceka = {cas: Date.now(), vyber: info.selectionText || '', tabId: tab ? tab.id : 0};
    otevrit(tab);
    B.storage.local.set({ceka});
  });
}
