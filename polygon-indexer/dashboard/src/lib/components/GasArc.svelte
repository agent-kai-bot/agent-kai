<script>
  import { formatNumeric } from '$lib/utils/format';

  export let overview = null;

  const centerX = 120;
  const centerY = 132;
  const radius = 72;
  const startAngle = 250;
  const endAngle = 470;
  const arcLength = Math.PI * radius * ((endAngle - startAngle) / 180);

  function polarToCartesian(cx, cy, r, angle) {
    const radians = ((angle - 90) * Math.PI) / 180;
    return {
      x: cx + r * Math.cos(radians),
      y: cy + r * Math.sin(radians)
    };
  }

  function describeArc(start, end) {
    const startPoint = polarToCartesian(centerX, centerY, radius, end);
    const endPoint = polarToCartesian(centerX, centerY, radius, start);
    const largeArcFlag = end - start <= 180 ? '0' : '1';
    return `M ${startPoint.x} ${startPoint.y} A ${radius} ${radius} 0 ${largeArcFlag} 0 ${endPoint.x} ${endPoint.y}`;
  }

  function interpolateColor(rank) {
    const clamped = Math.max(0, Math.min(1, Number(rank || 0)));
    if (clamped <= 0.5) {
      const ratio = clamped / 0.5;
      return mix('#22c55e', '#f59e0b', ratio);
    }
    return mix('#f59e0b', '#ef4444', (clamped - 0.5) / 0.5);
  }

  function mix(start, end, weight) {
    const startRgb = start.match(/\w\w/g).map((value) => Number.parseInt(value, 16));
    const endRgb = end.match(/\w\w/g).map((value) => Number.parseInt(value, 16));
    const mixed = startRgb.map((value, index) => Math.round(value + (endRgb[index] - value) * weight));
    return `rgb(${mixed.join(', ')})`;
  }

  $: progress = Math.max(0, Math.min(1, Number(overview?.gas_percentile_rank || 0)));
  $: activeColor = interpolateColor(progress);
  $: trackPath = describeArc(startAngle, endAngle);
</script>

<section class="panel gas-arc">
  <div class="header">
    <span class="eyebrow">Gas Arc</span>
  </div>

  <svg class="arc" viewBox="0 0 240 200" aria-hidden="true">
    <path d={trackPath} class="track" pathLength={arcLength}></path>
    <path
      d={trackPath}
      class="active"
      stroke={activeColor}
      pathLength={arcLength}
      stroke-dasharray={`${progress * arcLength} ${arcLength}`}
    ></path>
  </svg>

  <div class="center">
    <div class="value">{formatNumeric(overview?.gas_current_gwei || 0, 1)}</div>
    <div class="unit">gwei</div>
    <div class="subtitle">avg 20-block: {formatNumeric(overview?.gas_avg_20_block_gwei || 0, 1)} gwei</div>
  </div>
</section>

<style>
  .gas-arc {
    position: relative;
    height: 200px;
    padding: 16px;
    overflow: hidden;
  }

  .header {
    position: relative;
    z-index: 2;
  }

  .eyebrow {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-3);
  }

  .arc {
    position: absolute;
    inset: 14px 0 0;
    width: 100%;
    height: 172px;
  }

  .track,
  .active {
    fill: none;
    stroke-linecap: round;
    stroke-width: 10;
  }

  .track {
    stroke: rgba(148, 163, 184, 0.12);
  }

  .active {
    transition:
      stroke 200ms var(--ease-sharp),
      stroke-dasharray 200ms var(--ease-sharp);
  }

  .center {
    position: absolute;
    inset: 56px 16px 18px;
    display: grid;
    align-content: center;
    justify-items: center;
    gap: 2px;
    text-align: center;
  }

  .value {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 28px;
    font-weight: 600;
    line-height: 1;
  }

  .unit {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    color: var(--text-3);
    text-transform: uppercase;
    letter-spacing: 0.12em;
  }

  .subtitle {
    margin-top: 6px;
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 12px;
    color: var(--text-2);
  }
</style>
