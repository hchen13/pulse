from PIL import Image
img = Image.open('/Users/claire/.openclaw/media/browser/19dfac84-d90b-4911-9fab-112e6de6fa5b.jpg')
# Crop to first ~600px height (landscape ratio at 1280 wide)
w, h = img.size
# Scale: screenshot is 2x retina, so crop at 1200px height for 600px viewport
crop_h = min(1200, h)
cropped = img.crop((0, 0, w, crop_h))
cropped.save('pulse-dashboard-landscape.jpg', quality=85)
print(f"Cropped: {cropped.size}")
