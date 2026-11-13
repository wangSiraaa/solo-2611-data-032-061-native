from pathlib import Path
import argparse,random
def build(path, shuffled=False):
 pages=[
 [(40,800,'Synthetic expense report'),(40,760,'Date'),(155,760,'Description'),(455,760,'Amount'),
  (40,730,'2026-01-01'),(155,730,'Office supplies batch 12'),(455,730,'120.50'),
  (40,700,'2026-01-02'),(155,700,'Field visit on 2026-01-01'),(455,700,'-15.00'),
  (155,684,'includes route 42 materials'),(40,100,'2026-01-03'),(155,100,'Equipment maintenance and'),
  (40,45,'Page 1')],
 [(40,800,'Synthetic expense report'),(40,760,'Date'),(155,760,'Description'),(455,760,'Amount'),
  (155,730,'replacement components'),(455,730,'210.25'),
  (40,690,'2026-01-04'),(155,690,'Travel claim 2026-01-03'),(455,690,'55.00'),
  (155,674,'two receipts 18 and 19'),(40,625,'Table ends'),
  (40,590,'Note: 2026-01-05 and 999.99 describe next cycle, not a detail.'),
  (40,45,'Page 2')]]
 objects=[b'',b'']
 objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
 kids=[]
 for items in pages:
  if shuffled:random.Random(24).shuffle(items)
  def esc(s):return s.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')
  stream='\n'.join(f'BT /F1 11 Tf {x} {y} Td ({esc(t)}) Tj ET' for x,y,t in items).encode()
  content_id=len(objects)+1;objects.append(b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream')
  page_id=len(objects)+1;objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>'.encode());kids.append(page_id)
 objects[0]=b'<< /Type /Catalog /Pages 2 0 R >>'
 objects[1]=('<< /Type /Pages /Count '+str(len(kids))+' /Kids ['+' '.join(f'{k} 0 R' for k in kids)+'] >>').encode()
 out=bytearray(b'%PDF-1.4\n');offsets=[0]
 for i,obj in enumerate(objects,1):
  offsets.append(len(out));out.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
 start=len(out);out.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
 for o in offsets[1:]:out.extend(f'{o:010} 00000 n \n'.encode())
 out.extend(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
 Path(path).write_bytes(out)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('output');p.add_argument('--shuffle',action='store_true');a=p.parse_args();build(a.output,a.shuffle)
