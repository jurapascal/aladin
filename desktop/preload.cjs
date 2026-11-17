/* Most mezi stránkou hubu a appkou. Okno běží v sandboxu bez Node, takže
 * stránka dostane jen tohle: šablonu menu (hub ho nakreslí v liště malé —
 * nativní menu Electronu má na Linuxu a Windows položky velké jako pro prst)
 * a spuštění položky menu podle id. Nic dalšího ze systému stránka nevidí. */
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('hubApp', {
  menu: () => ipcRenderer.invoke('appka:menu'),
  akce: (id) => ipcRenderer.send('appka:akce', String(id)),
  // Stránka menu nakreslila → appka schová nativní lištu (Alt ji ukáže).
  menuVListe: (ano) => ipcRenderer.send('appka:menu-v-liste', !!ano),
  // Klik na oznámení → okno dopředu (i když je schované nebo minimalizované).
  ukaz: () => ipcRenderer.send('appka:ukaz'),
});
