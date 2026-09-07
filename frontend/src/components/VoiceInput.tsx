import { useState, useRef, useEffect, useCallback } from 'react'

interface VoiceInputProps {
  onTranscript: (text: string) => void
  onSubmit?: () => void
  disabled?: boolean
}

/**
 * Voice input ported from the AI-Research-Cowork app (originally from the
 * Coordination-App dashboard). Self-contained: uses the browser Web Speech API
 * for recognition plus getUserMedia + AnalyserNode for a live waveform.
 *
 * Behavior:
 * - Mic button starts continuous recognition; a recording bar overlays the input
 * - Cancel (✕) stops + clears, Submit (✓) stops + sends
 * - Clicking the mic again while recording submits
 * - Auto-restarts recognition on end (continuous mode)
 * - Beeps on start/cancel/submit; waveform scrolls right-to-left
 * - Renders nothing if the browser has no SpeechRecognition support
 */
export default function VoiceInput({ onTranscript, onSubmit, disabled }: VoiceInputProps) {
  const [listening, setListening] = useState(false)
  const [supported, setSupported] = useState(false)
  const [transcript, setTranscript] = useState('')
  const [showDeviceMenu, setShowDeviceMenu] = useState(false)
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([])
  const [selectedDevice, setSelectedDevice] = useState<string>(
    localStorage.getItem('dedup-mic-device') || ''
  )

  const recognitionRef = useRef<any>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const rafRef = useRef<number | null>(null)
  const fullTranscriptRef = useRef('')
  const isRecordingRef = useRef(false)

  useEffect(() => {
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    setSupported(!!SR)
  }, [])

  const playBeep = useCallback((freq: number, duration: number) => {
    try {
      const ctx = new (window.AudioContext || (window as any).webkitAudioContext)()
      const osc = ctx.createOscillator()
      const gain = ctx.createGain()
      osc.connect(gain)
      gain.connect(ctx.destination)
      osc.frequency.value = freq
      gain.gain.value = 0.15
      osc.start()
      osc.stop(ctx.currentTime + duration / 1000)
    } catch { /* ignore */ }
  }, [])

  const stopWaveform = useCallback(() => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current)
    if (streamRef.current) streamRef.current.getTracks().forEach(t => t.stop())
    if (audioCtxRef.current) audioCtxRef.current.close().catch(() => {})
    rafRef.current = null
    streamRef.current = null
    audioCtxRef.current = null
    if (canvasRef.current) {
      const ctx = canvasRef.current.getContext('2d')
      if (ctx) ctx.clearRect(0, 0, canvasRef.current.width, canvasRef.current.height)
    }
  }, [])

  const startWaveform = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const rect = canvas.getBoundingClientRect()
    canvas.width = rect.width * 2
    canvas.height = 48

    const historyLen = Math.ceil(canvas.width / 2)
    const history = new Float32Array(historyLen)

    const constraints: MediaStreamConstraints = {
      audio: selectedDevice ? { deviceId: { exact: selectedDevice } } : true,
    }

    navigator.mediaDevices.getUserMedia(constraints).then(stream => {
      streamRef.current = stream
      const audioCtx = new (window.AudioContext || (window as any).webkitAudioContext)()
      audioCtxRef.current = audioCtx
      const source = audioCtx.createMediaStreamSource(stream)
      const analyser = audioCtx.createAnalyser()
      analyser.fftSize = 256
      analyser.smoothingTimeConstant = 0.6
      source.connect(analyser)

      const data = new Float32Array(analyser.fftSize)
      const accent = getComputedStyle(document.documentElement)
        .getPropertyValue('--accent').trim() || '#4f9cf7'

      const draw = () => {
        if (!isRecordingRef.current) return
        rafRef.current = requestAnimationFrame(draw)
        analyser.getFloatTimeDomainData(data)

        let sum = 0
        for (let i = 0; i < data.length; i++) sum += data[i] * data[i]
        const rms = Math.sqrt(sum / data.length)
        const amplitude = Math.min(rms * 4, 1)

        for (let i = 0; i < historyLen - 1; i++) history[i] = history[i + 1]
        history[historyLen - 1] = amplitude

        const w = canvas.width, h = canvas.height
        ctx.clearRect(0, 0, w, h)
        const midY = h / 2

        ctx.beginPath()
        ctx.strokeStyle = 'rgba(127,127,127,0.3)'
        ctx.lineWidth = 1
        ctx.moveTo(0, midY)
        ctx.lineTo(w, midY)
        ctx.stroke()

        ctx.fillStyle = accent
        const barW = 2, gap = 1, step = barW + gap
        for (let i = 0; i < historyLen; i += step) {
          const v = history[i] || 0
          const barH = Math.max(v * h * 0.8, 1)
          const x = (i / historyLen) * w
          ctx.fillRect(x, midY - barH / 2, barW, barH)
        }
      }
      draw()
    }).catch(() => { /* mic access failed */ })
  }, [selectedDevice])

  const stopMicInternal = useCallback(() => {
    isRecordingRef.current = false
    setListening(false)
    if (recognitionRef.current) {
      try { recognitionRef.current.stop() } catch { /* ignore */ }
    }
    recognitionRef.current = null
    stopWaveform()
  }, [stopWaveform])

  const cancelMic = useCallback(() => {
    stopMicInternal()
    setTranscript('')
    fullTranscriptRef.current = ''
    onTranscript('')
    playBeep(330, 100)
  }, [stopMicInternal, onTranscript, playBeep])

  const submitMic = useCallback(() => {
    const text = fullTranscriptRef.current
    stopMicInternal()
    playBeep(660, 80)
    setTimeout(() => playBeep(880, 80), 100)
    if (text.trim()) {
      onTranscript(text.trim())
      if (onSubmit) setTimeout(onSubmit, 200)
    }
    setTranscript('')
    fullTranscriptRef.current = ''
  }, [stopMicInternal, onTranscript, onSubmit, playBeep])

  const startMic = useCallback(() => {
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    if (!SR) return

    fullTranscriptRef.current = ''
    setTranscript('')

    const recognition = new SR()
    recognition.continuous = true
    recognition.interimResults = true
    recognition.lang = localStorage.getItem('dedup-language') || 'en-US'

    recognition.onstart = () => {
      isRecordingRef.current = true
      setListening(true)
      startWaveform()
      playBeep(880, 120)
    }

    recognition.onresult = (ev: any) => {
      let interim = ''
      let final = ''
      for (let i = 0; i < ev.results.length; i++) {
        if (ev.results[i].isFinal) {
          final += ev.results[i][0].transcript
        } else {
          interim += ev.results[i][0].transcript
        }
      }
      fullTranscriptRef.current = final
      const display = final + interim
      setTranscript(display)
      onTranscript(display)
    }

    recognition.onend = () => {
      if (isRecordingRef.current) {
        try { recognition.start() } catch { /* ignore */ }
      }
    }

    recognition.onerror = (ev: any) => {
      if (ev.error === 'no-speech' || ev.error === 'aborted') return
      stopMicInternal()
    }

    recognitionRef.current = recognition
    recognition.start()
  }, [onTranscript, playBeep, startWaveform, stopMicInternal])

  const toggleMic = () => {
    if (disabled) return
    if (listening) submitMic()
    else startMic()
  }

  const toggleDeviceMenu = useCallback(async () => {
    if (showDeviceMenu) {
      setShowDeviceMenu(false)
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      stream.getTracks().forEach(t => t.stop())
      const allDevices = await navigator.mediaDevices.enumerateDevices()
      setDevices(allDevices.filter(d => d.kind === 'audioinput'))
      setShowDeviceMenu(true)
    } catch {
      setShowDeviceMenu(false)
    }
  }, [showDeviceMenu])

  const selectDevice = (deviceId: string) => {
    setSelectedDevice(deviceId)
    localStorage.setItem('dedup-mic-device', deviceId)
    setShowDeviceMenu(false)
  }

  // Clean up on unmount so recognition/mic don't leak across tab switches.
  useEffect(() => () => { stopMicInternal() }, [stopMicInternal])

  if (!supported) return null

  return (
    <div className="mic-area">
      <button
        className="btn-mic-menu"
        onClick={toggleDeviceMenu}
        title="Select microphone"
        disabled={disabled}
      >
        &#x25BC;
      </button>

      <button
        className={`btn-mic ${listening ? 'recording' : ''}`}
        onClick={toggleMic}
        disabled={disabled}
        title={listening ? 'Stop & submit' : 'Voice input'}
        aria-label={listening ? 'Stop voice input' : 'Start voice input'}
      >
        <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
          <path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm-1-9a1 1 0 1 1 2 0v6a1 1 0 1 1-2 0V5zm6 6c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" />
        </svg>
      </button>

      {showDeviceMenu && (
        <div className="mic-dropdown">
          {devices.map(d => (
            <div
              key={d.deviceId}
              className={`mic-option ${d.deviceId === selectedDevice ? 'active' : ''}`}
              onClick={() => selectDevice(d.deviceId)}
            >
              {d.label || `Microphone ${d.deviceId.substring(0, 8)}`}
            </div>
          ))}
        </div>
      )}

      {listening && (
        <div className="mic-recording-bar show">
          <div className="mic-transcript">{transcript || 'Listening…'}</div>
          <div className="mic-viz-row">
            <canvas ref={canvasRef} className="mic-canvas" />
            <button className="mic-cancel" onClick={cancelMic} title="Cancel">&#x2715;</button>
            <button className="mic-submit" onClick={submitMic} title="Submit">&#x2713;</button>
          </div>
        </div>
      )}
    </div>
  )
}
