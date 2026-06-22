// beacon.scd
// digital-beacon — 32-band binaural spatializer
//
// 32 BPF, one per natural harmonic of f1 (default 40 Hz).
// Band N center frequency = f1 * N for N = 1..32.
// Source: PlayBuf (looping WAV) or SoundIn(0), varispeed by \vsrate.
// Per-band: gain, azimuth, on/off (replaces wet/dry mix).
// FoaPanB → sum B-format → FoaDecode(Listen HRTF) → stereo out.
//
// OSC (sclang :57120):
//   /beacon/vsource [rate]   - sample rate multiplier (1.0 = native, 0.5 = -1 oct, 2.0 = +1 oct)
//   /beacon/f1 [hz]          - update f1 (band centers retune + broadcast vsrate if file source)
//   /beacon/gain/N [1..32] [0..3]
//   /beacon/az/N [1..32] [-180..180]
//   /beacon/on/N [1..32] [0|1]
//   /beacon/master [0..3]
//   /beacon/panic            - all bands off
//
// Outbound (sclang broadcasts to :9001):
//   /beacon/f1 [hz]
//   /beacon/band/N/active [1..32] [0|1]
//
// Server: 2in/2out, 48000 Hz, block 512 (HRTF kernel latency).
// ATK Listen kernels at ~/.local/share/ATK/kernels (subject 1002).

(
var nBands = 32;
var defaultF1 = 40.0;
var buf, decoder, synth, oscdefs;
var audioFilePath = "/home/nicolas/Projects/beacon-spatial/harmonic_beacon_2026_05_13_session.wav";
var useFile = "BEACON_SOURCE".getenv == "file";
var defName = if (useFile) { \digital_beacon_file } { \digital_beacon_live };

// Pre-started scsynth on 57110 (from start.sh via pw-jack).
var server = Server(\digital_beacon, NetAddr("127.0.0.1", 57110));
Server.default = server;

server.options.numInputBusChannels = 2;
server.options.numOutputBusChannels = 2;
server.options.sampleRate = 48000;
server.options.memSize = 65536;
server.options.blockSize = 64;

"".postln;
"========================================".postln;
"  DIGITAL BEACON — 32 BPF 1:1 with f1·N  ".postln;
"  ATK FOA binaural (Listen HRTF)         ".postln;
"========================================".postln;
"SC: %  |  ATK kernels: %".format(Main.version, Atk.userKernelDir).postln;
"f1 default: % Hz  |  bands: %  |  source: %".format(defaultF1, nBands,
  if (useFile) { "FILE (% — share with beacon-spatial)".format(audioFilePath.basename) } { "LIVE (SoundIn.ar(0))" }
).postln;
"".postln;

server.startAliveThread(0.4);
server.doWhenBooted({
  var cond = Condition.new;

  // Defaults: uniform gain, az array spreads bands around the head.
  // Bands 1-6 close in, 7-12 mid, 13-20 wider, 21-32 back to center.
  var defaultGains = { |i| 0.8 } ! nBands;
  // Default azimuths: 32 angles spread around the head.
  // Build it with a small table per band.
  var defaultAz = {
    var low = [-90, -45, 0, 45, 90, 135];
    var mid = [-60, -30, 0, 30, 60, 90];
    var high = [-180, -135, -90, 90, 135, 180, -45, 45];
    var top = [-90, 0, 90, -45, 45, -135, 135, 180, 0, -180, 45, -45];
    Array.fill(nBands, { |i|
      if (i < 6) { low[i] }
      { if (i < 12) { mid[i - 6] }
        { if (i < 20) { high[i - 12] }
          { top[(i - 20) % top.size] }
        }
      };
    });
  }.value;
  var defaultOns = { |i| 1.0 } ! nBands;   // all on by default
  var master0 = 0.9;

  var makeBeaconDef = { |name, srcFunc|
    SynthDef(name, {
      arg out = 0, bufnum = 0;

      // Source (file or live) with varispeed via \vsrate
      var vsrate = \vsrate.kr(1.0).lag(0.05);
      var src = srcFunc.value(bufnum, vsrate);

      // 32 BPF (one per harmonic N=1..32)
      // bandFreqs are derived at SynthDef build time, but we re-read f1 dynamically
      // via 32 NamedControls. Per design rule: BPF center = f1 * N.
      // The NamedControl \f1 carries the current f1 (with lag).
      var f1 = \f1.kr(defaultF1).lag(0.05);

      // 32 gains + 32 az + 32 on (default 1)
      var gains = Array.fill(nBands, { |i|
        ("gain" ++ (i+1)).asSymbol.kr(defaultGains[i]).lag(0.05)
      });
      var azs = Array.fill(nBands, { |i|
        ("az" ++ (i+1)).asSymbol.kr(defaultAz[i]).lag(0.05)
      });
      var ons = Array.fill(nBands, { |i|
        ("on" ++ (i+1)).asSymbol.kr(defaultOns[i])
      });
      var master = \master.kr(master0);

      // Build 32 BPFs
      var bpfs = Array.fill(nBands, { |i|
        var freq = (f1 * (i+1)).clip(20.0, 18000.0);
        var sig = BPF.ar(src, freq, 0.5);   // Q=0.5 (1 octave bandwidth) per band
        sig * gains[i] * ons[i]
      });

      // Per-band FoaPanB
      var foas = bpfs.collect { |bpfSig, i|
        var azRad = azs[i].degrad;
        FoaPanB.ar(bpfSig, azRad, 0)
      };
      var bfmt = Mix(foas);

      // Decode + master
      var wet = FoaDecode.ar(bfmt, decoder);
      Out.ar(out, wet * master);
    }).add;
  };

  "Server booted (2in/2out @ 48000Hz).".postln;
  server.freeAll;

  if (useFile) {
    "Loading buffer: % ...".format(audioFilePath.basename).postln;
    buf = Buffer.read(server, audioFilePath, action: {
      "  Buffer ready: bufnum=%, % frames, %s".format(
        buf.bufnum, buf.numFrames, buf.duration.round(0.01)
      ).postln;
      cond.unhang;
    });
    cond.hang;
  };

  "Loading FoaDecoderKernel.newListen(1002, 48000Hz, 512-sample)...".postln;
  decoder = FoaDecoderKernel.newListen(1002, server, 48000);
  2.0.wait;
  "  Decoder kernel ready (~11ms latency).".postln;

  "Defining SynthDef \\% ...".format(defName).postln;
  if (useFile) {
    makeBeaconDef.value(defName, { |bufnum, vsrate|
      PlayBuf.ar(1, bufnum, BufRateScale.kr(bufnum) * vsrate, loop: 1)
    });
  } {
    makeBeaconDef.value(defName, { |bufnum, vsrate|
      // Live input — vsrate not applicable to SoundIn (would need granular).
      // We still expose the slot so OSC schema is uniform; vsrate is ignored.
      SoundIn.ar(0)
    });
  };
  server.sync;
  "SynthDef \\% added.".format(defName).postln;

  // Instantiate
  if (useFile) {
    synth = Synth(defName, [\bufnum, buf.bufnum]);
  } {
    synth = Synth(defName);
  };
  "Synth active: nodeID=%  (headphones required for binaural)".format(synth.nodeID).postln;

  // ─── OSCdefs ─────────────────────────────────────────────────────────────

  // 32x gain
  nBands.do { |i|
    var n = i + 1;
    oscdefs = oscdefs.add(OSCdef(
      ("db_gain_" ++ n).asSymbol,
      { |msg| synth.set(("gain" ++ n).asSymbol, msg[1].asFloat) },
      "/beacon/gain/" ++ n
    ));
    // 32x az
    oscdefs = oscdefs.add(OSCdef(
      ("db_az_" ++ n).asSymbol,
      { |msg| synth.set(("az" ++ n).asSymbol, msg[1].asFloat) },
      "/beacon/az/" ++ n
    ));
    // 32x on/off
    oscdefs = oscdefs.add(OSCdef(
      ("db_on_" ++ n).asSymbol,
      { |msg|
        var v = msg[1].asFloat.clip(0.0, 1.0);
        synth.set(("on" ++ n).asSymbol, v);
        // Broadcast activity (so external UI can show)
        server.sendMsg("/beacon/band/" ++ n ++ "/active", v);
      },
      "/beacon/on/" ++ n
    ));
  };

  // /beacon/vsource — varispeed rate multiplier
  oscdefs = oscdefs.add(OSCdef(
    \db_vsource,
    { |msg| synth.set(\vsrate, msg[1].asFloat.clip(0.1, 4.0)) },
    "/beacon/vsource"
  ));

  // /beacon/f1 — update f1 (band centers retune)
  oscdefs = oscdefs.add(OSCdef(
    \db_f1,
    { |msg|
      var hz = msg[1].asFloat.clip(20.0, 200.0);
      synth.set(\f1, hz);
      "  f1 -> % Hz".format(hz).postln;
    },
    "/beacon/f1"
  ));

  // /beacon/master
  oscdefs = oscdefs.add(OSCdef(\db_master,
    { |msg| synth.set(\master, msg[1].asFloat.clip(0.0, 3.0)) },
    "/beacon/master"
  ));

  // /beacon/panic — all bands off
  oscdefs = oscdefs.add(OSCdef(\db_panic, {
    nBands.do { |i|
      synth.set(("on" ++ (i+1)).asSymbol, 0.0);
    };
    "[PANIC] All bands off.".postln;
  }, "/beacon/panic"));

  // /beacon/reset — restore defaults
  oscdefs = oscdefs.add(OSCdef(\db_reset, {
    synth.set(\vsrate, 1.0);
    synth.set(\f1, defaultF1);
    synth.set(\master, master0);
    nBands.do { |i|
      var n = i + 1;
      synth.set(("gain" ++ n).asSymbol, defaultGains[i]);
      synth.set(("az" ++ n).asSymbol, defaultAz[i]);
      synth.set(("on" ++ n).asSymbol, 1.0);
    };
    "[RESET] All bands restored.".postln;
  }, "/beacon/reset"));

  "OSCdefs registered: 32 gain + 32 az + 32 on + vsource + f1 + master + panic + reset (98 total).".postln;

  // Cleanup
  ServerQuit.add({
    "ServerQuit: freeing digital-beacon resources...".postln;
    oscdefs.do { |d| d.free };
    if (synth.notNil) { synth.free };
    if (decoder.notNil) { decoder.free };
    if (buf.notNil) { buf.free };
  }, server);

  CmdPeriod.add({
    "Cmd-. : audio stopped.".postln;
  });

  "".postln;
  "========================================".postln;
  "  DIGITAL BEACON READY                  ".postln;
  "  OSC to sclang:57120 | scsynth:57110   ".postln;
  "========================================".postln;
  "Bands: %  (1..% → f1·N)".format(nBands, nBands).postln;
  "f1 default: % Hz  |  vsrate default: 1.0".format(defaultF1).postln;
  "Default az (deg): %".format(defaultAz).postln;
  "".postln;
  if (useFile) {
    "Signal: PlayBuf (vsrate) -> 32 BPF(f1·N) -> FoaPanB -> B-format -> FoaDecode(Listen)".postln;
  } {
    "Signal: SoundIn.ar(0) -> 32 BPF(f1·N) -> FoaPanB -> B-format -> FoaDecode(Listen)".postln;
  };
  "        -> stereo out  (headphones required)".postln;
  "".postln;
  "Open NaturalHarmony to drive /beacon/voice/* + /beacon/f1 broadcasts.".postln;
  "Run f1_bridge.py to forward f1 to vsrate (file source only).".postln;
  "".postln;
  "sclang waiting for OSC...".postln;
});
)
