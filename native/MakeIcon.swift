import AppKit
import Foundation

guard CommandLine.arguments.count == 3 else {
    fputs("usage: MakeIcon.swift <server|lab> <output.png>\n", stderr)
    exit(2)
}
let mode = CommandLine.arguments[1]
let output = CommandLine.arguments[2]
let size = 1024
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                              bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                              isPlanar: false, colorSpaceName: .deviceRGB,
                              bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
let bounds = NSRect(x: 18, y: 18, width: 988, height: 988)
let background = NSBezierPath(roundedRect: bounds, xRadius: 190, yRadius: 190)
(mode == "server" ? NSColor(calibratedRed: 25/255, green: 39/255, blue: 128/255, alpha: 1)
                  : NSColor(calibratedRed: 18/255, green: 53/255, blue: 78/255, alpha: 1)).setFill()
background.fill()
let border = NSBezierPath(roundedRect: NSRect(x: 42, y: 42, width: 940, height: 940), xRadius: 170, yRadius: 170)
border.lineWidth = 20
NSColor(calibratedRed: 130/255, green: 1, blue: 246/255, alpha: 1).setStroke()
border.stroke()

let title = mode == "server" ? "8080" : "AXIS"
let font = NSFont.systemFont(ofSize: mode == "server" ? 315 : 270, weight: .black)
let paragraph = NSMutableParagraphStyle()
paragraph.alignment = .center
let attributes: [NSAttributedString.Key: Any] = [
    .font: font,
    .foregroundColor: NSColor(calibratedRed: 151/255, green: 1, blue: 250/255, alpha: 1),
    .paragraphStyle: paragraph,
]
let text = NSAttributedString(string: title, attributes: attributes)
let textSize = text.size()
text.draw(in: NSRect(x: 50, y: (1024 - textSize.height)/2 - 8,
                     width: 924, height: textSize.height + 16))
if mode == "lab" {
    NSColor(calibratedRed: 151/255, green: 1, blue: 250/255, alpha: 1).setFill()
    let bars: [CGFloat] = [84, 142, 108, 172, 118, 76]
    for (index, height) in bars.enumerated() {
        NSBezierPath(roundedRect: NSRect(x: 254 + CGFloat(index)*92, y: 220,
                                   width: 38, height: height), xRadius: 12, yRadius: 12).fill()
    }
}
NSGraphicsContext.restoreGraphicsState()
guard let data = bitmap.representation(using: .png, properties: [:]) else { exit(1) }
try data.write(to: URL(fileURLWithPath: output))
