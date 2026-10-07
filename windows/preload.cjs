'use strict';
const { contextBridge, ipcRenderer } = require('electron');
// Only the local settings BrowserWindow has this preload. Never expose generic IPC or filesystem access.
contextBridge.exposeInMainWorld('codexDAGSettings', Object.freeze({
  get: () => ipcRenderer.invoke('settings:get'),
  browseCodex: () => ipcRenderer.invoke('settings:browse', 'codexDir'),
  save: input => ipcRenderer.invoke('settings:save', {
    codexDir: input.codexDir, startOnLaunch: input.startOnLaunch, openWindowOnLaunch: input.openWindowOnLaunch
  })
}));
