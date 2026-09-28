package dev.lanpad;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.SystemClock;
import android.util.SparseIntArray;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;

import java.io.DataInputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Arrays;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * Full-screen, pitch-black touch surface that streams multitouch frames to the
 * lanpad.py daemon on the PC, over USB (TCP through `adb reverse`) when
 * plugged in, otherwise over Wi-Fi (UDP). Packets are authenticated with HMAC-SHA256.
 *
 * Wire format (big endian), every packet: "LPD1" | type:u8 | body | mac[16]
 * where mac = HMAC-SHA256(key, everything before it) truncated to 16 bytes.
 *   HELLO   phone->pc  nonce[8] session[8] (zeros if none) width:u16 height:u16
 *   WELCOME pc->phone  nonce[8] session[8]
 *   FRAME   phone->pc  session[8] seq:u32 count:u8 count*(id:u8 x:u16 y:u16)
 *   RESYNC  pc->phone  session[8]   (PC does not know this session any more)
 */
public class MainActivity extends Activity {
    static final int DEFAULT_PORT = 47474;
    static final byte[] MAGIC = {'L', 'P', 'D', '1'};
    static final byte HELLO = 1, WELCOME = 2, FRAME = 3, RESYNC = 4;
    static final int MAC_LEN = 16;
    static final long HEARTBEAT_MS = 2000, SEARCH_MS = 1000, LOST_AFTER_MS = 7000, RESEND_MS = 40;

    private SharedPreferences prefs;
    private TouchView view;
    private WifiManager.WifiLock wifiLock;

    // Networking; everything below is confined to the "net" thread.
    private HandlerThread netThread;
    private Handler net;
    private DatagramSocket socket;
    private Socket tcp;               // USB link through `adb reverse`, when plugged in
    private OutputStream tcpOut;
    private Mac mac;
    private int port;
    private InetAddress host;
    private byte[] session;
    private byte[] helloNonce;
    private int seq;
    private long lastWelcome;
    private long lastFrameSent;
    private boolean connected;
    private int viewW, viewH;
    private int[] curIds = new int[0], curXs = new int[0], curYs = new int[0];
    private final SecureRandom random = new SecureRandom();

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        prefs = getSharedPreferences("pairing", Context.MODE_PRIVATE);
        applyPairing(getIntent());
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS);
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
        }
        if (Build.VERSION.SDK_INT >= 28) {
            WindowManager.LayoutParams lp = getWindow().getAttributes();
            lp.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            getWindow().setAttributes(lp);
        }
        view = new TouchView(this);
        setContentView(view);
        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        int mode = Build.VERSION.SDK_INT >= 29 ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
        wifiLock = wm.createWifiLock(mode, "lanpad");
        wifiLock.setReferenceCounted(false);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (applyPairing(intent) && netThread != null) {
            stopNet();
            startNet();
        }
    }

    /** Pairing data is pushed by `lanpad.py pair` via adb as intent extras. */
    private boolean applyPairing(Intent intent) {
        String key = intent.getStringExtra("key");
        if (key == null) return false;
        SharedPreferences.Editor ed = prefs.edit().putString("key", key)
                .putInt("port", intent.getIntExtra("port", DEFAULT_PORT));
        String h = intent.getStringExtra("host");
        if (h != null) ed.putString("host", h);
        ed.apply();
        return true;
    }

    @Override
    protected void onResume() {
        super.onResume();
        hideSystemBars();
        wifiLock.acquire();
        startNet();
    }

    @Override
    protected void onPause() {
        stopNet();
        wifiLock.release();
        super.onPause();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) hideSystemBars();
    }

    @SuppressWarnings("deprecation")
    private void hideSystemBars() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
    }

    /** Backlight at minimum while connected (the screen is black anyway); normal while searching. */
    private void setConnectedUi(boolean on, String status) {
        runOnUiThread(() -> {
            WindowManager.LayoutParams lp = getWindow().getAttributes();
            lp.screenBrightness = on ? 0f : WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE;
            getWindow().setAttributes(lp);
            view.setStatus(status);
        });
    }

    // ---------------------------------------------------------------- network

    private void startNet() {
        String keyHex = prefs.getString("key", null);
        if (keyHex == null) {
            setConnectedUi(false, "Not paired. On the PC run:  lanpad.py pair");
            return;
        }
        netThread = new HandlerThread("net");
        netThread.start();
        net = new Handler(netThread.getLooper());
        final byte[] key = hexToBytes(keyHex);
        net.post(() -> {
            try {
                mac = Mac.getInstance("HmacSHA256");
                mac.init(new SecretKeySpec(key, "HmacSHA256"));
                port = prefs.getInt("port", DEFAULT_PORT);
                String h = prefs.getString("host", null);
                host = h != null ? InetAddress.getByName(h) : null;
                socket = new DatagramSocket();
                socket.setBroadcast(true);
            } catch (Exception e) {
                setConnectedUi(false, "Network error: " + e);
                return;
            }
            session = null;
            connected = false;
            setConnectedUi(false, "Looking for the PC...");
            final DatagramSocket sock = socket;
            final Handler h = net;
            Thread rx = new Thread(() -> receiveLoop(sock, h), "rx");
            rx.setDaemon(true);
            rx.start();
            Thread usb = new Thread(() -> usbLoop(sock, h), "usb");
            usb.setDaemon(true);
            usb.start();
            heartbeat.run();
        });
    }

    /** Lifts all fingers, closes the socket and waits for the net thread, so a following
     *  startNet() never races with the old thread. */
    private void stopNet() {
        if (netThread == null) return;
        final HandlerThread t = netThread;
        netThread = null;
        net.removeCallbacksAndMessages(null);
        net.post(() -> {
            curIds = new int[0];
            sendFrame();
            net.removeCallbacksAndMessages(null);
            if (socket != null) socket.close();  // also ends the rx and usb threads
            socket = null;
            closeTcp();
            t.quit();
        });
        try {
            t.join(500);
        } catch (InterruptedException ignored) {
        }
    }

    private void receiveLoop(DatagramSocket sock, Handler h) {
        byte[] buf = new byte[512];
        while (!sock.isClosed()) {
            DatagramPacket p = new DatagramPacket(buf, buf.length);
            try {
                sock.receive(p);
            } catch (Exception e) {
                return;
            }
            final byte[] data = Arrays.copyOf(p.getData(), p.getLength());
            final InetAddress from = p.getAddress();
            h.post(() -> onPacket(data, from));
        }
    }

    /**
     * USB transport: while the PC daemon keeps `adb reverse tcp:PORT tcp:PORT` set, a TCP
     * connection to localhost reaches it over the cable (no firewall involved). Packets are
     * the same as over UDP, each prefixed with a u16 length. Retries forever; lives as long
     * as this start's UDP socket.
     */
    private void usbLoop(DatagramSocket sock, Handler h) {
        while (!sock.isClosed()) {
            final Socket s = new Socket();
            try {
                s.setTcpNoDelay(true);
                s.connect(new InetSocketAddress("127.0.0.1", port), 500);
                DataInputStream in = new DataInputStream(s.getInputStream());
                h.post(() -> onTcpUp(s));
                while (!sock.isClosed()) {
                    byte[] data = new byte[in.readUnsignedShort()];
                    in.readFully(data);
                    h.post(() -> onPacket(data, null));
                }
            } catch (Exception ignored) {
                // not plugged in / daemon not running / cable dropped
            } finally {
                try {
                    s.close();
                } catch (Exception ignored) {
                }
                h.post(() -> onTcpDown(s));
            }
            SystemClock.sleep(1000);
        }
    }

    private void onTcpUp(Socket s) {
        if (socket == null) return;  // stopped meanwhile
        try {
            tcpOut = s.getOutputStream();
            tcp = s;
        } catch (Exception e) {
            return;
        }
        sendHello();
    }

    private void onTcpDown(Socket s) {
        if (tcp != s) return;
        closeTcp();
        // Fall back to Wi-Fi right away; the heartbeat flags the PC as lost only if that fails too.
        if (net != null) {
            net.removeCallbacks(heartbeat);
            heartbeat.run();
        }
    }

    private void closeTcp() {
        if (tcp != null) {
            try {
                tcp.close();
            } catch (Exception ignored) {
            }
        }
        tcp = null;
        tcpOut = null;
    }

    private void onPacket(byte[] data, InetAddress from) {
        if (socket == null || data.length < 5 + MAC_LEN || !verify(data)) return;
        ByteBuffer b = ByteBuffer.wrap(data, 5, data.length - 5 - MAC_LEN);
        if (data[4] == WELCOME && b.remaining() == 16) {
            byte[] nonce = new byte[8], sess = new byte[8];
            b.get(nonce).get(sess);
            if (helloNonce == null || !Arrays.equals(nonce, helloNonce)) return;
            session = sess;
            lastWelcome = SystemClock.uptimeMillis();
            if (from != null && !from.equals(host)) {  // from == null: came over USB
                host = from;
                prefs.edit().putString("host", from.getHostAddress()).apply();
            }
            if (!connected) {
                connected = true;
                setConnectedUi(true, "");
            }
        } else if (data[4] == RESYNC && b.remaining() == 8) {
            byte[] sess = new byte[8];
            b.get(sess);
            if (session != null && Arrays.equals(sess, session)) {
                session = null;
                net.removeCallbacks(heartbeat);
                heartbeat.run();
            }
        }
    }

    private final Runnable heartbeat = new Runnable() {
        @Override
        public void run() {
            if (connected && SystemClock.uptimeMillis() - lastWelcome > LOST_AFTER_MS) {
                connected = false;
                session = null;
                setConnectedUi(false, "Lost the PC, looking for it...");
            }
            sendHello();
            net.postDelayed(this, connected ? HEARTBEAT_MS : SEARCH_MS);
        }
    };

    private void sendHello() {
        if (viewW <= 0 || viewH <= 0) return;
        helloNonce = new byte[8];
        random.nextBytes(helloNonce);
        ByteBuffer b = packet(HELLO, 20);
        b.put(helloNonce).put(session != null ? session : new byte[8])
                .putShort((short) viewW).putShort((short) viewH);
        transmit(seal(b), true);
    }

    /** USB when linked, else Wi-Fi unicast to the known PC (plus broadcast for HELLOs while searching). */
    private void transmit(byte[] pkt, boolean hello) {
        if (tcpOut != null) {
            byte[] framed = new byte[2 + pkt.length];
            framed[0] = (byte) (pkt.length >> 8);
            framed[1] = (byte) pkt.length;
            System.arraycopy(pkt, 0, framed, 2, pkt.length);
            try {
                tcpOut.write(framed);
                return;
            } catch (Exception e) {
                closeTcp();
            }
        }
        if (host != null) send(pkt, host);
        if (hello && !connected) {
            try {
                send(pkt, InetAddress.getByName("255.255.255.255"));
            } catch (Exception ignored) {
            }
        }
    }

    private final Runnable resend = new Runnable() {
        @Override
        public void run() {
            if (curIds.length == 0) return;
            if (SystemClock.uptimeMillis() - lastFrameSent >= RESEND_MS) sendFrame();
            else net.postDelayed(this, RESEND_MS);
        }
    };

    private void sendFrame() {
        if (session == null || socket == null || (host == null && tcpOut == null)) return;
        int n = curIds.length;
        ByteBuffer b = packet(FRAME, 13 + n * 5);
        b.put(session).putInt(seq++).put((byte) n);
        for (int i = 0; i < n; i++) {
            b.put((byte) curIds[i]).putShort((short) curXs[i]).putShort((short) curYs[i]);
        }
        transmit(seal(b), false);
        lastFrameSent = SystemClock.uptimeMillis();
        net.removeCallbacks(resend);
        // While fingers are down keep repeating the state so the PC can tell a
        // resting finger from a lost connection.
        if (n > 0) net.postDelayed(resend, RESEND_MS);
    }

    /** Called on the UI thread for every touch event. */
    void onFrame(int[] ids, int[] xs, int[] ys) {
        Handler h = net;
        if (h == null) return;
        h.post(() -> {
            curIds = ids;
            curXs = xs;
            curYs = ys;
            sendFrame();
            if (ids.length == 0) {
                // UDP can drop the lift; repeat it so taps end promptly on the PC.
                h.postDelayed(() -> { if (curIds.length == 0) sendFrame(); }, 10);
                h.postDelayed(() -> { if (curIds.length == 0) sendFrame(); }, 25);
            }
        });
    }

    void onViewSize(int w, int h) {
        Handler n = net;
        if (n != null) n.post(() -> { viewW = w; viewH = h; sendHello(); });
        else { viewW = w; viewH = h; }
    }

    private ByteBuffer packet(byte type, int bodyLen) {
        ByteBuffer b = ByteBuffer.allocate(5 + bodyLen + MAC_LEN);
        b.put(MAGIC).put(type);
        return b;
    }

    private byte[] seal(ByteBuffer b) {
        byte[] a = b.array();
        int len = b.position();
        mac.update(a, 0, len);
        System.arraycopy(mac.doFinal(), 0, a, len, MAC_LEN);
        return Arrays.copyOf(a, len + MAC_LEN);
    }

    private boolean verify(byte[] d) {
        for (int i = 0; i < MAGIC.length; i++) if (d[i] != MAGIC[i]) return false;
        int len = d.length - MAC_LEN;
        mac.update(d, 0, len);
        byte[] want = Arrays.copyOf(mac.doFinal(), MAC_LEN);
        return MessageDigest.isEqual(want, Arrays.copyOfRange(d, len, d.length));
    }

    private void send(byte[] pkt, InetAddress to) {
        try {
            socket.send(new DatagramPacket(pkt, pkt.length, to, port));
        } catch (Exception ignored) {
            // Wi-Fi down etc.; the heartbeat keeps retrying.
        }
    }

    private static byte[] hexToBytes(String s) {
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(s.substring(2 * i, 2 * i + 2), 16);
        }
        return out;
    }

    // ---------------------------------------------------------------- view

    static class TouchView extends View {
        private final MainActivity act;
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final SparseIntArray trackOf = new SparseIntArray();
        private int nextTrack;
        private String status = "";

        TouchView(MainActivity act) {
            super(act);
            this.act = act;
            setBackgroundColor(Color.BLACK);
            paint.setColor(0xFF5A5A5A);
            paint.setTextAlign(Paint.Align.CENTER);
            paint.setTextSize(14 * getResources().getDisplayMetrics().scaledDensity);
        }

        void setStatus(String s) {
            status = s;
            invalidate();
        }

        @Override
        protected void onSizeChanged(int w, int h, int oldw, int oldh) {
            act.onViewSize(w, h);
        }

        @Override
        protected void onDraw(Canvas c) {
            if (!status.isEmpty()) c.drawText(status, getWidth() / 2f, getHeight() / 2f, paint);
        }

        @Override
        public boolean onTouchEvent(MotionEvent ev) {
            int action = ev.getActionMasked();
            int actionIndex = ev.getActionIndex();
            if (action == MotionEvent.ACTION_DOWN || action == MotionEvent.ACTION_POINTER_DOWN) {
                // Fresh id per finger-down, so the PC never mistakes a new touch for a jump.
                trackOf.put(ev.getPointerId(actionIndex), nextTrack);
                nextTrack = (nextTrack + 1) & 0xFF;
            }
            int n = ev.getPointerCount();
            int[] ids = new int[n], xs = new int[n], ys = new int[n];
            int k = 0;
            int w = getWidth(), h = getHeight();
            for (int i = 0; i < n; i++) {
                boolean lifted = action == MotionEvent.ACTION_CANCEL
                        || ((action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_POINTER_UP)
                        && i == actionIndex);
                int pid = ev.getPointerId(i);
                if (lifted) {
                    trackOf.delete(pid);
                    continue;
                }
                ids[k] = trackOf.get(pid);
                xs[k] = Math.max(0, Math.min(w - 1, Math.round(ev.getX(i))));
                ys[k] = Math.max(0, Math.min(h - 1, Math.round(ev.getY(i))));
                k++;
            }
            if (action == MotionEvent.ACTION_CANCEL || action == MotionEvent.ACTION_UP) trackOf.clear();
            act.onFrame(Arrays.copyOf(ids, k), Arrays.copyOf(xs, k), Arrays.copyOf(ys, k));
            return true;
        }
    }
}
