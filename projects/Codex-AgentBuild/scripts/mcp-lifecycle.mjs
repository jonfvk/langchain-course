// Only scalar lifecycle metadata crosses this boundary; no tokens or claims.
export function lifecycleEvent(phase, response, secrets=[]) {
  const result={kind:'http',phase,status:Number.isInteger(response?.status)?response.status:null};
  const retry=response?.headers?.['retry-after'];
  if (typeof retry==='string' || typeof retry==='number') {
    const numeric=Number(retry);
    const seconds=Number.isFinite(numeric)?numeric:Math.ceil((Date.parse(retry)-Date.now())/1000);
    if (Number.isFinite(seconds) && seconds>=0) result.retry_after_seconds=seconds;
  }
  let errorData=response?.data;
  if (typeof errorData==='string') {try {errorData=JSON.parse(errorData);} catch {errorData=null;}}
  function scrub(text) {
    if (typeof text!=='string') return undefined;
    for (const secret of secrets.filter(x=>typeof x==='string' && x.length)) text=text.split(secret).join('<REDACTED>');
    return text.replace(/(?:Bearer|Basic)\s+[^\s,;]+/gi,'<REDACTED>')
      .replace(/https?:\/\/[^\s]+/g,'<REDACTED>')
      .replace(/[A-Za-z0-9_+/=-]{24,}/g,'<REDACTED>')
      .replace(/[^\x20-\x7E]/g,' ').slice(0,500);
  }
  if (result.status>=400 && errorData && typeof errorData==='object') {
    const err=errorData.error;
    result.error_code=scrub(typeof err==='object'?err?.code:errorData.error_code??err);
    result.error_message=scrub(typeof err==='object'?err?.message:errorData.error_description??errorData.message);
    if (Array.isArray(errorData.errors)) result.error_details=errorData.errors.slice(0,5).map(e=>({code:scrub(e?.code),message:scrub(e?.message)}));
  }
  if (phase !== 'oauth') return result;
  let data=response?.data;
  if (typeof data === 'string') {try {data=JSON.parse(data);} catch {data=null;}}
  result.access_token_obtained=result.status===200 && typeof data?.access_token==='string' && data.access_token.length>0;
  const seconds=data?.expires_in;
  result.expires_in=typeof seconds==='number' && Number.isFinite(seconds) && seconds>0 && seconds<=86400?seconds:null;
  result.refresh_margin_seconds=result.expires_in ? Math.min(60,result.expires_in*.1):null;
  return result;
}
