"use strict";

// LSP4IJ may react to the DAP `initialized` event before its `attach` request
// has completed.  LuauDebugAdapter currently emits that event from
// initializeRequest, while its runtime object is only connected during
// attachRequest.  A concurrent setBreakpoints request therefore rejects with
// "ScriptDebugRuntime is not connected" and terminates the Node process.
//
// Load the external adapter without starting it, move the event to the end of
// a successful attach, then invoke its normal server entry point.  Keeping the
// compatibility code here avoids modifying DevelopPythonTools from this repo.

const path = require("path");

const adapterArgument = process.argv[2];
if (!adapterArgument) {
  throw new Error("LuauDebugAdapter entry path is required");
}

const adapterPath = path.resolve(adapterArgument);
const adapterDirectory = path.dirname(adapterPath);
const debugAdapterModule = require.resolve("vscode-debugadapter", {
  paths: [adapterDirectory],
});
const { DebugSession } = require(debugAdapterModule);

let AdapterSession;
const runAdapter = DebugSession.run;
DebugSession.run = (sessionType) => {
  AdapterSession = sessionType;
};
try {
  require(adapterPath);
} finally {
  DebugSession.run = runAdapter;
}

if (!AdapterSession) {
  throw new Error(`No DebugSession was exported by ${adapterPath}`);
}

const pendingInitializedEvent = Symbol("pendingInitializedEvent");
const initializeRequest = AdapterSession.prototype.initializeRequest;
const attachRequest = AdapterSession.prototype.attachRequest;

AdapterSession.prototype.initializeRequest = function (response, args) {
  const sendEvent = this.sendEvent;
  this.sendEvent = (event) => {
    if (event && event.event === "initialized") {
      this[pendingInitializedEvent] = event;
      return;
    }
    sendEvent.call(this, event);
  };
  try {
    return initializeRequest.call(this, response, args);
  } finally {
    this.sendEvent = sendEvent;
  }
};

AdapterSession.prototype.attachRequest = async function (response, args) {
  await attachRequest.call(this, response, args);
  const event = this[pendingInitializedEvent];
  if (event && response.success !== false) {
    delete this[pendingInitializedEvent];
    this.sendEvent(event);
  }
};

runAdapter(AdapterSession);
